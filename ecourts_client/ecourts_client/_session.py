"""eCourts mobile API session: JWT lifecycle + encrypted-GET transport.

Workflow:
    s = Session(scope="district")
    s.init()                          # bootstrap, mints initial JWT
    response_dict = s.call("stateWebService.php", {})

The cleartext request body carries the bundle-id ``uid`` plus endpoint-specific
fields. The whole body is encrypted into the request envelope and sent as
?params=<envelope> query string. The Bearer header on every non-bootstrap call
is the RAW JWT (v4.0; v3 wrapped it with encrypt_request).

Migrated to the eCourts mobile API v4.0 on 2026-07-02 -- see the block above the
DC_BASE_URL/HC_BASE_URL constants for the full v3->v4 delta and provenance
(eCourts Services v4.0.1 APK + live verification).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import requests

from ecourts_client._crypto import decrypt_response, encrypt_request
from ecourts_client.config import ECourtsConfig
from ecourts_client.errors import (
    BlockedByGeoIP,
    CourtSiteDown,
    ECourtsError,
    JWTExpired,
    RateLimited,
)


# A successful response is an AES envelope: 32 hex chars (the IV) followed by at
# least one base64 ciphertext char. Used to reject non-envelope bodies (HTML
# error/throttle pages, captchas, edge-proxy notices, or a lone 32-hex IV with no
# ciphertext) before they reach decrypt_response(), whose bytes.fromhex() would
# otherwise raise an opaque ValueError.
_RESPONSE_ENVELOPE_RE = re.compile(r"[0-9a-fA-F]{32}[A-Za-z0-9+/=]")


class _RateGate:
    """Process-wide minimum interval between outbound eCourts HTTP calls.

    eCourts rate-limits IP bursts to HTTP 405 + an HTML page for ~15-30 min
    (docs/RE_NOTES_v4.md). A bulk add/refresh fires one ``display_pdf_new.php``
    POST per order, so a book of cases can burst hundreds of calls in seconds and
    trip the throttle. This gate hands each caller a slot spaced ``min_interval``
    apart and sleeps until it -- capping the *rate* (the semaphore only caps
    concurrency). The lock is not held across the sleep, so concurrent callers
    reserve staggered slots rather than serialising on lock acquisition.

    ``monotonic``/``sleep`` are injectable so the slot arithmetic is unit-testable
    without real waiting.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            slot = max(self._monotonic(), self._next_allowed)
            self._next_allowed = slot + self.min_interval
        delay = slot - self._monotonic()
        if delay > 0:
            self._sleep(delay)


# One gate per process, created lazily on first send (NOT at import time, so a
# consumer that imports ecourts_client isn't coupled to ECourtsConfig()/env at
# import). Seeded from config (env: ECOURTS_MIN_REQUEST_INTERVAL_SECONDS).
_rate_gate: _RateGate | None = None
_rate_gate_lock = threading.Lock()


def _get_rate_gate() -> _RateGate:
    global _rate_gate
    if _rate_gate is None:
        with _rate_gate_lock:
            if _rate_gate is None:
                cfg = ECourtsConfig()
                if cfg.ecourts_use_redis_limiter:
                    # Import here (not module-top) so a consumer without the redis
                    # wheel is never coupled to it at import; the limiter itself
                    # lazy-imports redis and fails open to a _RateGate.
                    from ecourts_client.resilience.redis_limiter import RedisRateLimiter
                    _rate_gate = RedisRateLimiter(
                        cfg.ecourts_min_request_interval_seconds,
                        widen_factor=cfg.ecourts_redis_limiter_widen_factor,
                        max_interval=cfg.ecourts_redis_limiter_max_interval_seconds,
                        penalty_ttl_seconds=cfg.ecourts_redis_limiter_penalty_ttl_seconds,
                    )
                else:
                    _rate_gate = _RateGate(cfg.ecourts_min_request_interval_seconds)
    return _rate_gate


def _penalize_rate_gate() -> None:
    """On a 405, tell the active gate to back off. No-op unless the Redis limiter
    is active (a plain _RateGate has no shared state to widen). FULLY GUARDED: this
    runs at the 405 site immediately before ``raise RateLimited``, so a lazy limiter
    construction or attribute error here must NEVER escape and mask the RateLimited.

    Note the back-off is multiplicative PER 405 PER process: in a real IP-throttle
    burst every in-flight call on every process 405s, so the shared interval
    saturates to ``max_interval`` near-instantly (intended hard collective back-off;
    the interval key's TTL auto-resets it to base once the throttle clears)."""
    try:
        gate = _get_rate_gate()
        penalize = getattr(gate, "penalize", None)
        if penalize is not None:
            penalize()
    except Exception:  # noqa: BLE001 -- back-off is best-effort; never break transport
        pass


# Warm session registry: one long-lived Session per scope, reused across every
# fetch so the JWT is minted ~once per scope instead of once per operation (the
# appReleaseWebService.php flood behind the post-pacing throttle). Mirrors the
# _get_rate_gate singleton; the Session's own _lock makes concurrent mint/re-mint
# safe (see Session._ensure_jwt).
_warm_sessions: dict[Scope, Session] = {}
_warm_sessions_lock = threading.Lock()


def get_warm_session(scope: Scope) -> Session:
    """Return the process-wide warm Session for ``scope``, created lazily.

    Scope-keyed because a JWT minted against the DC service is invalid on HC.
    Expiry is REACTIVE only (re-mint on 401, see Session.call). FAST-FOLLOW
    EXTENSION POINT (not built): if a stale warm session is ever seen to fail
    WITHOUT a clean 401, add a wall-clock TTL or evict-on-repeated-failure here
    — this is the single place to bound a bad session's lifetime.
    """
    s = _warm_sessions.get(scope)
    if s is None:
        with _warm_sessions_lock:
            s = _warm_sessions.get(scope)
            if s is None:
                s = Session(scope=scope)
                _warm_sessions[scope] = s
    return s


def reset_warm_sessions() -> None:
    """Clear the warm-session registry. Tests only — module-level state would
    otherwise leak a minted JWT / _http between tests."""
    with _warm_sessions_lock:
        _warm_sessions.clear()


logger = logging.getLogger(__name__)

# Throttle observability (audit finding: eCourts throttling was invisible --
# RateLimited/CourtSiteDown were raised silently, so frequency couldn't be
# measured). Every classification site emits ONE greppable WARNING carrying a
# stable ``ECOURTS_THROTTLE`` token + a ``kind=`` tag, so
# ``docker logs <c> | grep ECOURTS_THROTTLE`` yields a per-hour, per-kind tally
# across all containers with no new hard dependency. A best-effort Redis
# hour-bucket counter adds cross-process aggregation where Redis is reachable.
_THROTTLE_COUNTER_TTL_SECONDS = 8 * 24 * 3600  # keep ~8 days of hour buckets
_throttle_redis_singleton: Any = None


def _throttle_redis_client() -> Any:
    """Lazily build a best-effort Redis client from ``SHARED_REDIS_URL`` /
    ``REDIS_URL``. Returns a client or None. The ``redis`` import is LAZY so
    environments without the wheel (e.g. casepilot) never import-fail; short
    socket timeouts keep a down/slow Redis off the throttle path's latency."""
    global _throttle_redis_singleton
    if _throttle_redis_singleton is not None:
        return _throttle_redis_singleton
    url = os.environ.get("SHARED_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        return None
    import redis  # lazy: casepilot has no redis wheel -> ImportError swallowed by caller

    _throttle_redis_singleton = redis.Redis.from_url(
        url, socket_connect_timeout=0.25, socket_timeout=0.25,
    )
    return _throttle_redis_singleton


def _record_throttle_event(kind: str, endpoint: str) -> None:
    """Best-effort per-hour, per-kind Redis counter. FAIL-OPEN: any error (redis
    absent, unreachable, or slow) is a silent no-op -- observability must never
    mask the real RateLimited/CourtSiteDown the caller has to see."""
    try:
        client = _throttle_redis_client()
        if client is None:
            return
        bucket = time.strftime("%Y%m%d%H", time.gmtime())
        key = f"ecourts:throttle:{bucket}:{kind}"
        client.incr(key)
        client.expire(key, _THROTTLE_COUNTER_TTL_SECONDS)
    except Exception:  # noqa: BLE001 -- observability must never break transport
        pass


def _mint_cooldown_key(scope: str) -> str:
    return f"ecourts:mint_cooldown:{scope}"


def _shared_mint_cooldown_remaining(scope: str) -> float:
    """Seconds left on the FLEET-WIDE mint cooldown, or 0.0 if none/unknown.

    The per-instance ``_mint_cooldown_until`` cannot coordinate a fleet: RQ forks
    a process per job, so every fresh process arrived with a clean window and was
    free to re-hit the bootstrap -- observed as mint-405 bursts spaced 14-20s
    apart, well inside the 30s window. Redis makes the window shared.

    FAIL-OPEN: any error (no redis wheel, no URL, unreachable) returns 0.0 and the
    caller falls back to the in-process window -- i.e. exactly today's behaviour."""
    try:
        client = _throttle_redis_client()
        if client is None:
            return 0.0
        pttl = client.pttl(_mint_cooldown_key(scope))
        # redis-py: -2 = no key, -1 = key without expiry. Neither is a live window.
        if pttl is None or pttl < 0:
            return 0.0
        return float(pttl) / 1000.0
    except Exception:  # noqa: BLE001 -- guard must never mask the real RateLimited
        return 0.0


def _arm_shared_mint_cooldown(scope: str) -> None:
    """Publish the mint cooldown fleet-wide. Best-effort/FAIL-OPEN: if Redis is
    unreachable the local window still applies, so behaviour degrades to today's
    per-process guard rather than breaking the mint path."""
    try:
        client = _throttle_redis_client()
        if client is None:
            return
        client.set(
            _mint_cooldown_key(scope),
            "1",
            px=max(1, int(_MINT_COOLDOWN_SECONDS * 1000)),
        )
    except Exception:  # noqa: BLE001 -- guard must never mask the real RateLimited
        pass


# Fleet-wide JWT cache. ``get_warm_session`` keeps one Session (one JWT) per
# PROCESS, which is not enough for the bot: ``rq.Worker`` forks a child per job
# and the parent never runs job code, so the warm registry is built in a child
# that immediately exits -- every job cold-minted. Sharing the token is sound
# because nothing in the v4.0 mint is per-process (payload is
# {"appVersion": <const>, "uid": <bare bundle id>}, uid CONSTANT), and one token
# is already shared across every thread in a process today.
_JWT_EXP_MARGIN_SECONDS = 60.0    # stop serving a token this long before it dies
# Ceiling for an implausible/hostile ``exp``. Sized from a REAL prod token
# (captured 2026-07-20): eCourts v4.0 issues exp - iat = 6010s (~100 min), so a
# 1h ceiling would clip every real token and force a needless early re-mint.
_JWT_MAX_TTL_SECONDS = 7200.0
_JWT_FALLBACK_TTL_SECONDS = 300.0  # used when ``exp`` can't be parsed


def _jwt_cache_key(scope: str) -> str:
    return f"ecourts:jwt:{scope}"


def _jwt_seconds_remaining(token: str) -> float | None:
    """Seconds until the token's ``exp``, or None if it can't be read. The
    signature is NOT verified -- eCourts is the only verifier; we read ``exp``
    solely to size the cache TTL, so no crypto dependency is introduced."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped padding
        exp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp")
        if exp is None:
            return None
        return float(exp) - time.time()
    except Exception:  # noqa: BLE001 -- an unparseable token must not break minting
        return None


def _shared_jwt_get(scope: str) -> str | None:
    """Return a live fleet-wide token, or None. FAIL-OPEN: any error yields None
    and the caller mints exactly as it does today."""
    try:
        client = _throttle_redis_client()
        if client is None:
            return None
        raw = client.get(_jwt_cache_key(scope))
        if not raw:
            return None
        token = raw.decode() if isinstance(raw, bytes) else str(raw)
        # Defence in depth: Redis TTL should already have evicted an expired
        # token, but re-check so clock skew can never serve a dead one.
        remaining = _jwt_seconds_remaining(token)
        if remaining is not None and remaining <= _JWT_EXP_MARGIN_SECONDS:
            return None
        return token
    except Exception:  # noqa: BLE001
        return None


def _shared_jwt_put(scope: str, token: str) -> None:
    """Publish a freshly minted token fleet-wide, TTL sized from its own ``exp``.
    Best-effort: a failure here just means siblings mint their own."""
    try:
        client = _throttle_redis_client()
        if client is None:
            return
        remaining = _jwt_seconds_remaining(token)
        if remaining is None:
            ttl = _JWT_FALLBACK_TTL_SECONDS
        else:
            ttl = min(remaining - _JWT_EXP_MARGIN_SECONDS, _JWT_MAX_TTL_SECONDS)
        if ttl <= 0:
            return  # already expired (or within the margin) -- never publish it
        client.set(_jwt_cache_key(scope), token, px=max(1, int(ttl * 1000)))
    except Exception:  # noqa: BLE001
        pass


# COMPARE-AND-DELETE. A plain DEL would let a process arriving late with a 401
# for an OLD token wipe the brand-new one a sibling just published -- costing the
# fleet a good token and sending every process back to the mint endpoint.
_JWT_CAD_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _shared_jwt_drop(scope: str, token: str) -> bool:
    """Invalidate the shared token, but ONLY if it is still the one we used.

    Returns True when the cache is known NOT to still hold ``token`` -- i.e. we
    deleted it, or someone had already replaced it. Returns False when we could
    not establish that (no Redis, or the EVAL failed).

    The return value is load-bearing, which is why this one Redis helper is not
    purely fail-open in the caller's eyes: a drop that silently failed would let
    ``init()`` read the rejected token straight back out of the cache and hand it
    to the caller, wedging the session with zero mints. The caller uses False to
    bypass the cache and force a real mint."""
    try:
        client = _throttle_redis_client()
        if client is None:
            return False
        client.eval(_JWT_CAD_LUA, 1, _jwt_cache_key(scope), token)
        return True
    except Exception:  # noqa: BLE001 -- never raise into the mint path
        return False


def _log_throttle(kind: str, endpoint: str, status: int) -> None:
    """Emit the greppable WARNING + bump the best-effort Redis counter."""
    logger.warning("ECOURTS_THROTTLE kind=%s endpoint=%s http=%s", kind, endpoint, status)
    _record_throttle_event(kind, endpoint)


# --- eCourts mobile API v4.0 (migrated 2026-07-02) --------------------------
# eCourts retired the v3.0 mobile endpoints (``/ecourt_mobile_DC/`` and
# ``/ecourt_mobile_HC/`` -> HTTP 404) and moved to the v4.0 service paths.
# Verified against the eCourts Services v4.0.1 APK (Hermes RN bundle) + live:
#   * base URL          : /services_{DC,HC}_4.0/  (was /ecourt_mobile_{DC,HC}/)
#   * bootstrap payload : {"appVersion": "4.0.1", "uid": <bundle id>}
#                         (v3 sent {"version": ..., "uid": "<uuid>:<pkg>"})
#   * bearer            : "Bearer <raw jwt>"  (v3 wrapped the jwt with
#                         encrypt_request via wrap_bearer -> now "UnAuthorized")
#   * common envelope   : the app's request interceptor injects ``uid`` (the
#                         bare bundle id) into every request's params.
#   * AES envelope keys : UNCHANGED (request + response crypto identical).
DC_BASE_URL = "https://app.ecourts.gov.in/services_DC_4.0/"
HC_BASE_URL = "https://app.ecourts.gov.in/services_HC_4.0/"

_USER_AGENT = "eCourts-Bot/0.1 (+https://github.com/yourorg/ecourts-bot)"
_PACKAGE_NAME = "in.gov.ecourts.eCourtsServices"
# v4.0: the ``uid`` on every request is the bare bundle id (NOT ``<uuid>:<pkg>``
# as in v3). Sending the v3 form makes appReleaseWebService.php withhold the
# token (``version_compatible:"S1", token:null``) and data calls 401.
_UID = _PACKAGE_NAME
_APP_VERSION = "4.0.1"
_REQUEST_TIMEOUT = 30

# Mint-flood guard: a 405/429 on the JWT-mint bootstrap (appReleaseWebService.php)
# leaves ``self.jwt`` None, so ``_ensure_jwt`` would re-attempt the mint on EVERY
# subsequent call -- hammering eCourts' most burst-sensitive endpoint and turning
# a transient throttle into a sustained appReleaseWebService.php outage (which
# then trips the shared ``ecourts_global`` circuit platform-wide). After a mint
# throttle we hold off further mint attempts for this window so callers fast-fail
# with RateLimited instead of re-flooding the bootstrap. Kept short so recovery
# stays inside the circuit's base recovery window.
_MINT_COOLDOWN_SECONDS = float(os.environ.get("ECOURTS_MINT_COOLDOWN_SECONDS", "30"))

# A-5 audit fix: the transport layer is now retry-free. The outer
# ``ecourts_client.resilience.retry.with_retry`` decorator (composed by
# ``client._wrap_with_resilience``) is the single source of retry truth and
# catches ``CourtSiteDown``. Removing the inner loop avoids the multiplicative
# blow-up (inner 4 * outer 3 = 12 attempts under a sticky 5xx) that starved
# the bot-session pool during eCourts brownouts.
#
# This module continues to *classify* transport failures as ``CourtSiteDown``
# (or ``RateLimited`` / ``BlockedByGeoIP``); the outer policy decides whether
# they are retryable.


Scope = Literal["district", "highcourt"]


@dataclass
class Session:
    """One-instance HTTP session with rolling JWT and encrypted-GET transport."""

    scope: Scope
    base_url: str = field(init=False)
    jwt: str | None = field(init=False, default=None)
    uid: str = field(init=False)
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = DC_BASE_URL if self.scope == "district" else HC_BASE_URL
        # v4.0: uid is the bare bundle id (constant); the per-session identity is
        # the JWT itself (each init() mints a fresh iat/nbf/exp token). The old
        # per-instance uuid is no longer part of the uid.
        self.uid = _UID
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _USER_AGENT})
        # Warm sessions are shared across threads (asyncio.to_thread pool +
        # casepilot concurrent ingest). This RLock serializes ONLY the JWT mint;
        # _mint_gen converges concurrent 401s to a single re-mint. Invariant:
        # self.jwt is written only in init(), under this lock, never lock-free.
        self._lock = threading.RLock()
        self._mint_gen = 0
        # Set to a monotonic deadline after a mint 405/429; while in the future,
        # init() fast-fails instead of re-hitting the throttled bootstrap.
        self._mint_cooldown_until = 0.0

    @property
    def uid_with_pkgname(self) -> str:
        """v4.0 uid == the bare bundle id (kept as a property for call sites)."""
        return self.uid

    def init(self, *, use_shared: bool = True) -> None:
        """Mint the initial JWT via appReleaseWebService.php (v4.0).

        ``use_shared=False`` skips the fleet-wide cache and forces a real mint.
        The 401 path uses it when it could not confirm the rejected token was
        evicted, so correctness never depends on the eviction having worked.

        v4.0 bootstrap payload is ``{"appVersion": <ver>, "uid": <bundle id>}``.
        The response carries the HS256 JWT in ``token`` (same slot as v3). Sending
        the v3 shape (``version`` + ``<uuid>:<pkg>`` uid) makes the server return
        ``version_compatible:"S1", token:null``.

        Serialized under ``self._lock`` and the SOLE writer of ``self.jwt`` — this
        is what makes a shared warm Session thread-safe. Do NOT wrap the RateGate
        sleep inside a second lock; ``_send`` gates itself and this stays deadlock-free.
        """
        with self._lock:
            # A live fleet-wide token means no mint is needed AT ALL, so this is
            # checked BEFORE the cooldown: the cooldown guards the bootstrap
            # endpoint, not token use. Checking it first would fast-fail callers
            # we could serve from cache -- and would bite hardest during a
            # throttle, precisely when the cached token is most valuable.
            shared = _shared_jwt_get(self.scope) if use_shared else None
            if shared:
                self.jwt = shared
                self._mint_gen += 1
                return
            now = time.monotonic()
            # Local window AND the fleet-wide one: whichever has longer to run
            # wins. The shared window is what stops a *different* process (RQ
            # forks one per job) from re-hitting a bootstrap we already know is
            # throttled; the local one still applies if Redis is unavailable.
            local_remaining = self._mint_cooldown_until - now
            shared_remaining = _shared_mint_cooldown_remaining(self.scope)
            remaining = max(local_remaining, shared_remaining)
            if remaining > 0:
                # A recent mint got 405/429-throttled. Do NOT re-hit the bootstrap
                # endpoint on every call -- that self-sustaining flood is exactly
                # what keeps appReleaseWebService.php throttled. Fast-fail instead;
                # the outer resilience layer (circuit + retry) handles back-off.
                scope_note = "fleet" if shared_remaining >= local_remaining else "local"
                raise RateLimited(
                    "JWT mint (appReleaseWebService.php) on cooldown for "
                    f"{remaining:.0f}s ({scope_note}) after a recent "
                    "405/429 -- not re-hitting the throttle-sensitive bootstrap."
                )
            payload = {"appVersion": _APP_VERSION, "uid": self.uid}
            try:
                body = self._send("appReleaseWebService.php", payload, with_bearer=False)
            except RateLimited:
                # The mint endpoint itself is burst-throttled. Arm the cooldown so
                # subsequent callers fast-fail above rather than re-flooding it --
                # locally AND fleet-wide, so sibling processes back off too.
                self._mint_cooldown_until = time.monotonic() + _MINT_COOLDOWN_SECONDS
                _arm_shared_mint_cooldown(self.scope)
                raise
            token = body.get("token")
            if not token:
                raise ECourtsError(
                    f"appReleaseWebService.php returned no token: {body!r}. "
                    "Check appVersion/uid (v4.0 wants appVersion + bare-bundle-id uid)."
                )
            self.jwt = token
            self._mint_gen += 1
            _shared_jwt_put(self.scope, token)

    def _ensure_jwt(self) -> None:
        """Mint the JWT if absent, double-checked under the lock so concurrent
        cold callers on a shared warm session mint exactly once."""
        if self.jwt is None:
            with self._lock:
                if self.jwt is None:
                    self.init()

    def _remint_on_401(self, gen_used: int) -> None:
        """Re-mint after a 401, converging concurrent 401s: only the first thread
        (whose request used the still-current generation) re-mints; the rest reuse
        the freshly minted jwt."""
        with self._lock:
            if self._mint_gen == gen_used:
                # eCourts rejected this token, so the fleet copy is dead too --
                # invalidate it (compare-and-delete) BEFORE re-minting, or init()
                # would just re-adopt the very token that was refused.
                dropped = True
                if self.jwt is not None:
                    dropped = _shared_jwt_drop(self.scope, self.jwt)
                # If the eviction could not be confirmed, do NOT consult the cache
                # on the way back in -- it may still hold the token eCourts just
                # refused, and re-adopting it would wedge this session into
                # repeated 401s with zero mints.
                self.init(use_shared=dropped)

    def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Encrypted-GET to <base>/<endpoint>.

        The payload is sent as-is (no common envelope auto-injected -- each endpoint
        has its own required fields per the JS source). Handles a single 401 retry
        with the package-name-suffixed uid; raises JWTExpired on a second 401.
        """
        self._ensure_jwt()
        gen = self._mint_gen

        result = self._send(endpoint, payload, with_bearer=True)

        # v4.0: a 401 ("UnAuthorized" / "Not in session") means the JWT expired or
        # was rejected -> mint a fresh token via init() and retry once.
        if result.get("status") == "N" and str(result.get("status_code")) == "401":
            self._remint_on_401(gen)
            result = self._send(endpoint, payload, with_bearer=True)
            if result.get("status") == "N" and str(result.get("status_code")) == "401":
                raise JWTExpired(f"Second 401 from {endpoint}; JWT must be re-minted")

        if result.get("status") == "N":
            # v4.0 uses "Msg"; v3 used "msg"/"errorMessage".
            msg = (
                result.get("Msg")
                or result.get("msg")
                or result.get("errorMessage")
                or "unknown eCourts error"
            )
            raise ECourtsError(f"{endpoint}: {msg}")

        return result

    def _send(self, endpoint: str, payload: Any, *, with_bearer: bool, method: str = "GET") -> dict[str, Any]:
        url = self.base_url + endpoint

        # v4.0: the app's request interceptor injects the bundle-id ``uid`` into
        # every request's params. Mirror that here so per-endpoint callers don't
        # each have to remember it.
        if isinstance(payload, dict) and "uid" not in payload:
            payload = {**payload, "uid": self.uid}
        encrypted_body = encrypt_request(payload)

        headers: dict[str, str] = {}
        if with_bearer:
            tok = self.jwt  # snapshot once: never re-read self.jwt between the guard and use
            if tok is None:
                raise ECourtsError("attempt to send authenticated call without JWT")
            # v4.0: the JWT is sent RAW (v3 wrapped it via encrypt_request).
            headers["Authorization"] = f"Bearer {tok}"

        # A-5: no inner retry loop. Classify the failure and let the outer
        # ``with_retry`` decorator decide. Transport-level connection errors
        # and HTTP 5xx both become ``CourtSiteDown`` and are retryable; 429
        # and GeoIP-403 are *not* retryable (no rationale to hammer the
        # same throttled endpoint or relocate the egress IP).
        # Proactive burst control: space outbound eCourts calls so a bulk
        # add/refresh (one display_pdf_new.php POST per order) can't trip the
        # 405/HTML throttle. No-op when the interval is 0. Placed here so it
        # gates every wire call (bootstrap, search, fetch, PDF POST) uniformly.
        _get_rate_gate().wait()

        try:
            # v4.0: most endpoints are encrypted-GET; display_pdf_new.php is an
            # encrypted-POST (params still on the query string). Keep the plain
            # ``.get`` for the GET path (test doubles stub ``.get``); use
            # ``.request`` only for the rarer POST.
            if method == "GET":
                resp = self._http.get(
                    url, params={"params": encrypted_body}, headers=headers, timeout=_REQUEST_TIMEOUT,
                )
            else:
                resp = self._http.request(
                    method, url, params={"params": encrypted_body}, headers=headers, timeout=_REQUEST_TIMEOUT,
                )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"connection error on {endpoint}: {e}") from e

        if resp.status_code == 429:
            _log_throttle("throttle_429", endpoint, 429)
            raise RateLimited(f"eCourts returned 429 for {endpoint}")
        if resp.status_code == 403 and "geographic" in resp.text.lower():
            _log_throttle("geoip_403", endpoint, 403)
            raise BlockedByGeoIP(f"GeoIP block for {endpoint}")
        if resp.status_code == 405:
            # eCourts throttles IP bursts to HTTP 405 + an HTML "Search Page not
            # Found" page for ~15-30 min (docs/RE_NOTES_v4.md). Classify as
            # RateLimited -- NOT CourtSiteDown -- so the outer with_retry policy
            # (which retries CourtSiteDown 3x) does not hammer the throttle and
            # reset its window. The shared "ecourts_global" circuit breaker still
            # trips after the failure threshold, backing the whole process off
            # until it clears.
            _log_throttle("throttle_405", endpoint, 405)
            _penalize_rate_gate()
            raise RateLimited(f"eCourts returned 405 (burst throttle) for {endpoint}")
        if 500 <= resp.status_code < 600:
            _log_throttle("http_5xx", endpoint, resp.status_code)
            raise CourtSiteDown(f"{resp.status_code} on {endpoint}")

        # eCourts returns plaintext JSON on validation/auth errors (e.g. {"status":"N","msg":"ERROR"})
        # and an encrypted envelope on success. Detect the cleartext path by trying to parse
        # the response as-is before falling through to decryption.
        body = resp.text.strip()
        if body.startswith("{") and body.endswith("}"):
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                pass  # fall through to decrypt — may genuinely be ciphertext that happens to start with {

        # Guard decrypt_response against non-envelope bodies. A success response
        # is 32 hex chars (IV) + base64 ciphertext; anything else here -- an HTML
        # error/throttle/maintenance page, a captcha, an edge-proxy notice --
        # would make decrypt_response's bytes.fromhex() raise an opaque
        # ValueError (which the API layer turns into a confusing 500). Surface it
        # as a transient CourtSiteDown with a short diagnostic snippet instead.
        if not _RESPONSE_ENVELOPE_RE.match(body):
            snippet = " ".join(body[:160].split())
            _log_throttle("non_envelope", endpoint, resp.status_code)
            raise CourtSiteDown(
                f"non-envelope response from {endpoint} "
                f"(HTTP {resp.status_code}, {len(body)} bytes): {snippet!r}"
            )

        plaintext = decrypt_response(resp.text)
        try:
            parsed = json.loads(plaintext)
        except json.JSONDecodeError as e:
            raise ECourtsError(f"non-JSON response from {endpoint}: {plaintext[:200]!r}") from e

        # NOTE: self.jwt is minted ONLY in init() under _lock (shared-Session
        # invariant). We deliberately do NOT rotate jwt from data responses here;
        # a stale token self-heals via the reactive 401 re-mint in call().
        return parsed
