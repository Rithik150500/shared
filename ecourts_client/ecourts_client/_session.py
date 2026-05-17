"""eCourts mobile API session: JWT lifecycle + encrypted-GET transport.

Workflow:
    s = Session(scope="district")
    s.init()                          # bootstrap, mints initial JWT
    response_dict = s.call("stateWebService.php", {})

The cleartext request body always carries the common envelope (version_number, uid,
language_code) plus endpoint-specific fields. The whole body is encrypted into the
request envelope and sent as ?params=<envelope> query string. The Bearer header
on every non-bootstrap call is wrap_bearer(jwt) -- the JWT is itself encrypted
before being placed in the Bearer slot.

See docs/RE_NOTES.md sections 2.4 and 2.5 for the full provenance.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import requests

from ecourts_client._crypto import decrypt_response, encrypt_request, wrap_bearer
from ecourts_client.errors import (
    BlockedByGeoIP,
    CourtSiteDown,
    ECourtsError,
    RateLimited,
)


DC_BASE_URL = "https://app.ecourts.gov.in/ecourt_mobile_DC/"
HC_BASE_URL = "https://app.ecourts.gov.in/ecourt_mobile_HC/"

_USER_AGENT = "eCourts-Bot/0.1 (+https://github.com/yourorg/ecourts-bot)"
_PACKAGE_NAME = "in.gov.ecourts.eCourtsServices"
_APP_VERSION = "3.0"
_REQUEST_TIMEOUT = 30
_MAX_5XX_RETRIES = 3
_BACKOFF_BASE = 1.0


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
        self.uid = str(uuid.uuid4())
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _USER_AGENT})

    @property
    def uid_with_pkgname(self) -> str:
        """The uid format the server expects: <UUID>:<pkgname>."""
        return f"{self.uid}:{_PACKAGE_NAME}"

    def init(self) -> None:
        """Mint the initial JWT via appReleaseWebService.php.

        Per index.js:63 the bootstrap payload is exactly {version, uid} where uid
        already includes the package-name suffix.
        """
        payload = {"version": _APP_VERSION, "uid": self.uid_with_pkgname}
        body = self._send("appReleaseWebService.php", payload, with_bearer=False)
        token = body.get("token")
        if not token:
            # Some server responses include only appReleaseObj/version_compatible and a null
            # token; in that case we cannot proceed with authenticated calls.
            raise ECourtsError(
                f"appReleaseWebService.php returned no token: {body!r}. "
                "JWT-issuing init may require additional fields or a separate endpoint."
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

        if result.get("status") == "N" and str(result.get("status_code")) == "401":
            retried = {**payload, "uid": self.uid_with_pkgname}
            result = self._send(endpoint, retried, with_bearer=True)
            if result.get("status") == "N" and str(result.get("status_code")) == "401":
                self.jwt = None
                raise JWTExpired(f"Second 401 from {endpoint}; JWT must be re-minted")

        if result.get("status") == "N":
            msg = result.get("msg") or result.get("errorMessage") or "unknown eCourts error"
            raise ECourtsError(f"{endpoint}: {msg}")

        return result

    def _send(self, endpoint: str, payload: Any, *, with_bearer: bool) -> dict[str, Any]:
        url = self.base_url + endpoint
        encrypted_body = encrypt_request(payload)

        headers: dict[str, str] = {}
        if with_bearer:
            if self.jwt is None:
                raise ECourtsError("attempt to send authenticated call without JWT")
            headers["Authorization"] = f"Bearer {wrap_bearer(self.jwt)}"

        last_exc: Exception | None = None
        for attempt in range(_MAX_5XX_RETRIES + 1):
            try:
                resp = self._http.get(
                    url,
                    params={"params": encrypted_body},
                    headers=headers,
                    timeout=_REQUEST_TIMEOUT,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt < _MAX_5XX_RETRIES:
                    time.sleep(_BACKOFF_BASE * (2**attempt))
                    continue
                raise CourtSiteDown(f"connection error after {_MAX_5XX_RETRIES} retries: {e}") from e

            if resp.status_code == 429:
                raise RateLimited(f"eCourts returned 429 for {endpoint}")
            if resp.status_code == 403 and "geographic" in resp.text.lower():
                raise BlockedByGeoIP(f"GeoIP block for {endpoint}")
            if 500 <= resp.status_code < 600:
                if attempt < _MAX_5XX_RETRIES:
                    time.sleep(_BACKOFF_BASE * (2**attempt))
                    continue
                raise CourtSiteDown(f"{resp.status_code} after {_MAX_5XX_RETRIES} retries on {endpoint}")
            break
        else:
            raise CourtSiteDown("retries exhausted") from last_exc

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
