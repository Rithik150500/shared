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

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import requests

from ecourts_client._crypto import decrypt_response, encrypt_request
from ecourts_client.errors import (
    BlockedByGeoIP,
    CourtSiteDown,
    ECourtsError,
    RateLimited,
)


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


class JWTExpired(ECourtsError):
    """Two consecutive 401 responses; caller must mint a fresh JWT (call init again)."""


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

    @property
    def uid_with_pkgname(self) -> str:
        """v4.0 uid == the bare bundle id (kept as a property for call sites)."""
        return self.uid

    def init(self) -> None:
        """Mint the initial JWT via appReleaseWebService.php (v4.0).

        v4.0 bootstrap payload is ``{"appVersion": <ver>, "uid": <bundle id>}``.
        The response carries the HS256 JWT in ``token`` (same slot as v3). Sending
        the v3 shape (``version`` + ``<uuid>:<pkg>`` uid) makes the server return
        ``version_compatible:"S1", token:null``.
        """
        payload = {"appVersion": _APP_VERSION, "uid": self.uid}
        body = self._send("appReleaseWebService.php", payload, with_bearer=False)
        token = body.get("token")
        if not token:
            raise ECourtsError(
                f"appReleaseWebService.php returned no token: {body!r}. "
                "Check appVersion/uid (v4.0 wants appVersion + bare-bundle-id uid)."
            )
        self.jwt = token

    def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Encrypted-GET to <base>/<endpoint>.

        The payload is sent as-is (no common envelope auto-injected -- each endpoint
        has its own required fields per the JS source). Handles a single 401 retry
        with the package-name-suffixed uid; raises JWTExpired on a second 401.
        """
        if self.jwt is None:
            self.init()

        result = self._send(endpoint, payload, with_bearer=True)

        # v4.0: a 401 ("UnAuthorized" / "Not in session") means the JWT expired or
        # was rejected -> mint a fresh token via init() and retry once.
        if result.get("status") == "N" and str(result.get("status_code")) == "401":
            self.jwt = None
            self.init()
            result = self._send(endpoint, payload, with_bearer=True)
            if result.get("status") == "N" and str(result.get("status_code")) == "401":
                self.jwt = None
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

    def _send(self, endpoint: str, payload: Any, *, with_bearer: bool) -> dict[str, Any]:
        url = self.base_url + endpoint

        # v4.0: the app's request interceptor injects the bundle-id ``uid`` into
        # every request's params. Mirror that here so per-endpoint callers don't
        # each have to remember it.
        if isinstance(payload, dict) and "uid" not in payload:
            payload = {**payload, "uid": self.uid}
        encrypted_body = encrypt_request(payload)

        headers: dict[str, str] = {}
        if with_bearer:
            if self.jwt is None:
                raise ECourtsError("attempt to send authenticated call without JWT")
            # v4.0: the JWT is sent RAW (v3 wrapped it via encrypt_request).
            headers["Authorization"] = f"Bearer {self.jwt}"

        # A-5: no inner retry loop. Classify the failure and let the outer
        # ``with_retry`` decorator decide. Transport-level connection errors
        # and HTTP 5xx both become ``CourtSiteDown`` and are retryable; 429
        # and GeoIP-403 are *not* retryable (no rationale to hammer the
        # same throttled endpoint or relocate the egress IP).
        try:
            resp = self._http.get(
                url,
                params={"params": encrypted_body},
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"connection error on {endpoint}: {e}") from e

        if resp.status_code == 429:
            raise RateLimited(f"eCourts returned 429 for {endpoint}")
        if resp.status_code == 403 and "geographic" in resp.text.lower():
            raise BlockedByGeoIP(f"GeoIP block for {endpoint}")
        if 500 <= resp.status_code < 600:
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

        plaintext = decrypt_response(resp.text)
        try:
            parsed = json.loads(plaintext)
        except json.JSONDecodeError as e:
            raise ECourtsError(f"non-JSON response from {endpoint}: {plaintext[:200]!r}") from e

        if "token" in parsed:
            self.jwt = parsed["token"]
        return parsed
