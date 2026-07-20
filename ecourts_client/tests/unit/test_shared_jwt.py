"""Fleet-wide JWT sharing for ecourts_client._session.Session.

``get_warm_session`` keeps ONE Session (and therefore one JWT) per process. That
is not enough for the bot: ``rq.Worker`` forks a child process per job, and the
parent never runs job code, so the warm registry is populated in a child that
then exits. Every job therefore cold-minted a fresh JWT against
appReleaseWebService.php -- eCourts' most burst-sensitive endpoint -- which is
what produced 94/94 mint-endpoint 405s on the 2026-07-19->20 poll wave.

Sharing the token across processes is sound because nothing in the v4.0 mint is
per-process: the payload is {"appVersion": <const>, "uid": <bare bundle id>},
and the uid is a CONSTANT. A token minted by any process is indistinguishable
from one minted by another, and the warm-session design already shares one token
across every thread in a process.

All Redis use here is FAIL-OPEN: losing Redis degrades to today's per-process
mint, never to a broken mint path.
"""
from __future__ import annotations

import base64
import itertools
import json
import time

import pytest

from ecourts_client import _session as S
from ecourts_client.errors import RateLimited

REDIS_URL = "redis://localhost:6379/15"


_jti = itertools.count()


def _make_jwt(exp_in: float = 3600.0) -> str:
    """A structurally real HS256 JWT (signature is irrelevant -- we never verify;
    only the ``exp`` claim is read, to size the cache TTL).

    The ``jti`` counter matters: iat/exp are whole seconds, so without it two
    calls in the same second produce BYTE-IDENTICAL tokens and any test that
    needs two *distinct* tokens silently tests nothing."""
    def b64(d: dict) -> str:
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    head = b64({"alg": "HS256", "typ": "JWT"})
    body = b64({
        "iat": int(time.time()),
        "exp": int(time.time() + exp_in),
        "jti": next(_jti),
    })
    return f"{head}.{body}.sig"


@pytest.fixture()
def redis_env(monkeypatch):
    r = pytest.importorskip("redis")
    client = r.Redis.from_url(REDIS_URL, socket_connect_timeout=0.25, socket_timeout=0.25)
    try:
        client.ping()
    except Exception:
        pytest.skip("no local Redis on db 15")
    client.flushdb()
    monkeypatch.setenv("SHARED_REDIS_URL", REDIS_URL)
    monkeypatch.setattr(S, "_throttle_redis_singleton", None)
    yield client
    client.flushdb()


def _minting_send(calls, token):
    def _send(endpoint, payload, *, with_bearer):
        calls.append(endpoint)
        return {"token": token}
    return _send


def test_second_process_adopts_the_shared_jwt_instead_of_minting(redis_env):
    """THE FIX: once ANY process has minted, a second process must reuse that
    token rather than hitting appReleaseWebService.php again."""
    token = _make_jwt()
    calls = []

    first = S.Session(scope="highcourt")
    first._send = _minting_send(calls, token)
    first.init()
    assert first.jwt == token
    assert calls == ["appReleaseWebService.php"], "first process mints exactly once"

    second = S.Session(scope="highcourt")            # simulates a forked RQ child
    second._send = _minting_send(calls, "SHOULD-NOT-BE-MINTED")
    second.init()

    assert second.jwt == token, "second process must adopt the shared token"
    assert calls == ["appReleaseWebService.php"], (
        "a forked job must NOT re-mint -- that per-job cold mint is exactly what "
        "flooded appReleaseWebService.php during the poll wave"
    )


def test_shared_jwt_is_scoped(redis_env):
    """A DC token is invalid on HC, so the cache must not leak across scopes."""
    dc_token = _make_jwt()
    calls = []
    dc = S.Session(scope="district")
    dc._send = _minting_send(calls, dc_token)
    dc.init()

    hc_token = _make_jwt()
    hc = S.Session(scope="highcourt")
    hc._send = _minting_send(calls, hc_token)
    hc.init()

    assert hc.jwt == hc_token, "highcourt must mint its own token, not reuse district's"
    assert len(calls) == 2


def test_expired_shared_jwt_is_not_served(redis_env):
    """A cached token past its exp must never be handed out -- that would turn one
    stale token into a fleet-wide 401 storm."""
    stale = _make_jwt(exp_in=-10.0)                  # already expired
    S._shared_jwt_put("highcourt", stale)
    assert S._shared_jwt_get("highcourt") is None

    fresh = _make_jwt()
    calls = []
    sess = S.Session(scope="highcourt")
    sess._send = _minting_send(calls, fresh)
    sess.init()
    assert sess.jwt == fresh, "an expired cache entry must fall through to a mint"


def test_401_drops_the_shared_jwt_so_the_fleet_stops_using_it(redis_env):
    """When eCourts rejects the token, the shared copy must be invalidated or
    every other process keeps presenting a token known to be dead."""
    bad = _make_jwt()
    S._shared_jwt_put("highcourt", bad)
    assert S._shared_jwt_get("highcourt") == bad

    good = _make_jwt()
    calls = []
    sess = S.Session(scope="highcourt")
    sess.init()                                       # adopts `bad` from the cache
    assert sess.jwt == bad
    sess._send = _minting_send(calls, good)
    sess._remint_on_401(sess._mint_gen)

    assert sess.jwt == good
    assert S._shared_jwt_get("highcourt") == good, (
        "the rejected token must be replaced fleet-wide, not left for siblings"
    )


def test_compare_and_drop_does_not_stomp_a_newer_token(redis_env):
    """Two processes 401 on the same dead token. The first re-mints and publishes;
    the second must NOT then delete that brand-new token on its way past."""
    dead, fresh = _make_jwt(), _make_jwt()
    S._shared_jwt_put("highcourt", fresh)             # a sibling already re-minted
    S._shared_jwt_drop("highcourt", dead)             # late 401 for the OLD token
    assert S._shared_jwt_get("highcourt") == fresh, (
        "dropping must be compare-and-delete, else the fleet loses a good token "
        "and every process re-mints -- the flood we are trying to prevent"
    )


def test_shared_jwt_is_used_even_while_the_mint_is_on_cooldown(redis_env):
    """The mint cooldown guards MINTING, not token use. If a live shared token
    exists there is no need to touch the bootstrap at all, so a cooldown must not
    fast-fail the caller -- doing so would fail requests we could serve, and
    would bite hardest exactly during a throttle, when it matters most."""
    token = _make_jwt()
    S._shared_jwt_put("highcourt", token)

    sess = S.Session(scope="highcourt")
    sess._mint_cooldown_until = time.monotonic() + 30.0     # cooldown armed
    S._arm_shared_mint_cooldown("highcourt")                # fleet cooldown too

    def _boom(endpoint, payload, *, with_bearer):
        raise AssertionError("must not hit the bootstrap when a token is cached")
    sess._send = _boom

    sess.init()
    assert sess.jwt == token, "a cached token must be usable during a mint cooldown"


def test_real_shaped_token_is_cached_for_close_to_its_full_life(redis_env):
    """A REAL eCourts v4.0 token (captured from prod 2026-07-20) carries
    exp - iat = 6010s, i.e. ~100 minutes. The TTL ceiling must not clip that down
    to an hour -- doing so re-mints ~40 minutes early for no reason, and the whole
    point of this change is to mint as rarely as possible."""
    token = _make_jwt(exp_in=6010.0)
    S._shared_jwt_put("highcourt", token)
    pttl_ms = redis_env.pttl(S._jwt_cache_key("highcourt"))
    assert pttl_ms > 3600 * 1000, (
        f"a ~100min token was cached for only {pttl_ms/1000:.0f}s -- the ceiling "
        "is clipping real tokens and forcing premature mints"
    )
    assert pttl_ms < 6010 * 1000, "must still expire before the token itself does"


def test_mint_gen_advances_when_a_shared_token_is_adopted(redis_env):
    """``_mint_gen`` is what makes concurrent 401s converge on ONE re-mint:
    call() snapshots it, and _remint_on_401 only re-mints if it still matches.
    The adopt path must bump it exactly like the mint path, or a caller holding a
    stale generation re-mints needlessly. Deleting that one line left the whole
    suite green, so this pins it."""
    S._shared_jwt_put("highcourt", _make_jwt())
    sess = S.Session(scope="highcourt")
    before = sess._mint_gen
    sess.init()                                    # adopts from cache, does not mint
    assert sess._mint_gen == before + 1, (
        "adopting a shared token must advance the generation counter, exactly as "
        "minting does -- otherwise 401 convergence silently breaks"
    )


def test_failed_drop_forces_a_mint_instead_of_re_adopting_the_dead_token(redis_env, monkeypatch):
    """If the compare-and-delete cannot run (Redis with scripting disabled/ACL'd,
    or a transient EVAL error), the dead token stays cached -- and init() would
    read it straight back, hand the caller the very token eCourts just refused,
    and never mint. That wedges the session: repeated calls, zero mints. The drop
    must therefore report failure, and the re-mint path must then bypass the cache."""
    dead = _make_jwt()
    S._shared_jwt_put("highcourt", dead)

    real_client = S._throttle_redis_client()

    class _NoEval:
        def __getattr__(self, name):
            if name == "eval":
                def _refuse(*a, **k):
                    raise RuntimeError("EVAL refused (scripting disabled)")
                return _refuse
            return getattr(real_client, name)

    monkeypatch.setattr(S, "_throttle_redis_singleton", _NoEval())

    good = _make_jwt()
    calls = []
    sess = S.Session(scope="highcourt")
    sess.init()                                    # adopts `dead`
    assert sess.jwt == dead
    sess._send = _minting_send(calls, good)
    sess._remint_on_401(sess._mint_gen)

    assert calls == ["appReleaseWebService.php"], (
        "a drop that could not run must force a real mint -- re-adopting the "
        "rejected token wedges the session with zero mints"
    )
    assert sess.jwt == good


def test_jwt_sharing_fails_open_when_redis_is_unreachable(monkeypatch):
    """Redis down must degrade to today's per-process mint, not break minting."""
    monkeypatch.setenv("SHARED_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(S, "_throttle_redis_singleton", None)

    assert S._shared_jwt_get("highcourt") is None
    S._shared_jwt_put("highcourt", _make_jwt())       # must not raise
    S._shared_jwt_drop("highcourt", "whatever")       # must not raise

    token = _make_jwt()
    calls = []
    sess = S.Session(scope="highcourt")
    sess._send = _minting_send(calls, token)
    sess.init()
    assert sess.jwt == token and calls == ["appReleaseWebService.php"]
