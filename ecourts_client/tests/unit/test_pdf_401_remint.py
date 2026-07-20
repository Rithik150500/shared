"""``fetch_order_pdf`` must recover from a 401 the same way ``Session.call`` does.

``fetch_order_pdf`` POSTs display_pdf_new.php through ``session._send`` DIRECTLY,
bypassing ``Session.call`` -- and therefore bypassing call()'s 401 -> re-mint
retry. A 401 there just produced a response with no ``pdf_url``, surfacing as a
plain PDFNotFound while the dead token stayed in place.

That was survivable while the JWT was per-process: only that one process held the
dead token, and its next call() would re-mint. Once the token is shared fleet-wide
it is NOT survivable -- the dead token sits in Redis and every other process
adopts it, so one 401 on the PDF path can fail order fetches across the whole
fleet until the cache entry expires. Sharing the token amplified this, so the PDF
path now has to invalidate and re-mint like everything else.
"""
from __future__ import annotations

import base64
import itertools
import json
import time

import pytest

from ecourts_client import _session as S
from ecourts_client.pdf import fetch_order_pdf

REDIS_URL = "redis://localhost:6379/15"
_UNAUTHORIZED = {"status": "N", "status_code": "401", "Msg": "UnAuthorized"}
_jti = itertools.count()


def _make_jwt(exp_in: float = 3600.0) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return "{}.{}.sig".format(
        b64({"alg": "HS256", "typ": "JWT"}),
        b64({"iat": int(time.time()), "exp": int(time.time() + exp_in), "jti": next(_jti)}),
    )


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


def test_pdf_401_reminits_and_clears_the_shared_token(redis_env, monkeypatch):
    dead, good = _make_jwt(), _make_jwt()
    S._shared_jwt_put("highcourt", dead)

    sess = S.Session(scope="highcourt")
    sent, minted = [], []

    def _send(endpoint, payload, *, with_bearer, method=None):
        if endpoint == "appReleaseWebService.php":
            minted.append(endpoint)
            return {"token": good}
        sent.append(sess.jwt)                       # which token went on the wire
        return _UNAUTHORIZED if sess.jwt == dead else {"pdf_url": "https://x/y.pdf"}

    sess._send = _send
    monkeypatch.setattr("ecourts_client.pdf.fetch_pdf", lambda http, url: b"%PDF-1.4 ok")

    out = fetch_order_pdf(sess, "displaypdf:cino=ABC&order_no=1")

    assert out == b"%PDF-1.4 ok", "a 401 on the PDF path must recover, not raise"
    assert minted == ["appReleaseWebService.php"], "the dead token must trigger ONE re-mint"
    assert sent == [dead, good], "the retry must present the NEW token"
    assert S._shared_jwt_get("highcourt") == good, (
        "the rejected token must be evicted fleet-wide -- otherwise every other "
        "process adopts a token known to be dead"
    )
