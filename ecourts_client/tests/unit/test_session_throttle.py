"""eCourts throttles IP bursts to HTTP 405 + an HTML error page for ~15-30 min
(see docs/RE_NOTES_v4.md, "Live tests must be rate-limited").

Before this fix, a 405/HTML response fell through ``_send``'s status checks into
``decrypt_response()``, which did ``bytes.fromhex("<!DOCTYPE...")`` and raised an
opaque ``ValueError: non-hexadecimal number found in fromhex()``. The API layer's
``except Exception`` turned that into a confusing HTTP 500 "Internal error during
search"; and had it been (mis)classified as ``CourtSiteDown``, the outer
``with_retry`` would have hammered the throttle 3x and *prolonged* it.

These tests pin the post-fix contract:
  * 405/HTML                       -> RateLimited (NOT retried; trips the shared
                                      circuit breaker after the failure threshold)
  * any other non-envelope body    -> CourtSiteDown (transient upstream failure)
  * a valid AES response envelope  -> decrypts normally (no regression)
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ecourts_client._crypto import RESPONSE_KEY
from ecourts_client._session import Session
from ecourts_client.errors import CourtSiteDown, RateLimited
from ecourts_client.resilience.retry import with_retry


# The exact page eCourts serves to a throttled IP (captured live 2026-07-03).
_THROTTLE_HTML = (
    '<!DOCTYPE html>\r\n<html>\r\n<body>\n<base href="/" />\n'
    '<script type="text/javascript">\n  var _event_transid=\'2808555848\';\n</script>\n'
    '<center><strong>Welcome User Search Page not Found here</strong></center>\r\n'
    '</body>\r\n</html>\r\n'
)


class _CannedHTTP:
    """Stand-in for ``requests.Session`` returning a fixed status+body, counting
    calls so we can assert the retry fan-out."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self._text = text
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, *args, **kwargs):
        self.calls += 1
        resp = requests.Response()
        resp.status_code = self.status_code
        resp._content = self._text.encode("utf-8")
        return resp


def _sess() -> Session:
    s = Session(scope="district")
    s.jwt = "fake-jwt-for-test"  # skip bootstrap; go straight to _send
    return s


def _make_envelope(payload: dict) -> str:
    """Build a real response envelope: iv_hex(32) || base64(AES-128-CBC ct)."""
    plaintext = json.dumps(payload).encode("utf-8")
    iv = bytes.fromhex("00112233445566778899aabbccddeeff")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(RESPONSE_KEY), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return iv.hex() + base64.b64encode(ct).decode("ascii")


def test_405_html_is_ratelimited_not_fromhex():
    """A 405 throttle page raises RateLimited, never a bare ValueError."""
    s = _sess()
    s._http = _CannedHTTP(405, _THROTTLE_HTML)  # type: ignore[assignment]
    with pytest.raises(RateLimited):
        s._send("searchByPartyName.php", {}, with_bearer=True)


def test_405_throttle_is_not_retried_by_outer_policy():
    """RateLimited is not in _RETRIABLE, so exactly ONE HTTP hit reaches the
    wire -- hammering a throttle would reset eCourts' 15-30 min window."""
    s = _sess()
    fake = _CannedHTTP(405, _THROTTLE_HTML)
    s._http = fake  # type: ignore[assignment]

    @with_retry(max_attempts=3, base_delay=0.005)
    async def call_async():
        return s.call("searchByPartyName.php", {})

    with pytest.raises(RateLimited):
        asyncio.run(call_async())

    assert fake.calls == 1, (
        f"throttle produced {fake.calls} HTTP requests; a 405 must NOT be retried"
    )


def test_200_html_non_envelope_is_courtsitedown():
    """A non-405 non-envelope body (e.g. a 200 maintenance page) is a transient
    CourtSiteDown -- still never an opaque fromhex ValueError."""
    s = _sess()
    s._http = _CannedHTTP(200, "<html><body>site under maintenance</body></html>")  # type: ignore[assignment]
    with pytest.raises(CourtSiteDown):
        s._send("stateWebService.php", {}, with_bearer=True)


def test_valid_envelope_still_decrypts():
    """The AES-envelope success path is unaffected by the new guards."""
    payload = {"states": [{"name": "Gujarat", "code": "17"}]}
    s = _sess()
    s._http = _CannedHTTP(200, _make_envelope(payload))  # type: ignore[assignment]
    assert s._send("stateWebService.php", {}, with_bearer=True) == payload


def test_cleartext_json_error_still_passes_through():
    """eCourts' plaintext ``{"status":"N",...}`` errors must still parse as JSON
    (not be swallowed by the envelope guard)."""
    s = _sess()
    s._http = _CannedHTTP(200, '{"status":"N","Msg":"INVALID"}')  # type: ignore[assignment]
    assert s._send("stateWebService.php", {}, with_bearer=True) == {
        "status": "N",
        "Msg": "INVALID",
    }
