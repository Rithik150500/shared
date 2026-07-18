"""Mint-flood guard for ecourts_client._session.Session.

A 405 on the JWT-mint bootstrap (appReleaseWebService.php) leaves self.jwt None,
so _ensure_jwt would re-attempt the mint on EVERY subsequent call, hammering the
most throttle-sensitive endpoint and turning a transient burst into a sustained
outage. After a mint 405/429 the session must hold off further mint attempts for
a cooldown window (callers fast-fail with RateLimited instead of re-flooding).
"""
import pytest
from ecourts_client import _session as S
from ecourts_client.errors import RateLimited


def _throttled_send(calls):
    def _send(endpoint, payload, *, with_bearer):
        calls.append(endpoint)
        raise RateLimited("eCourts returned 405 (burst throttle) for appReleaseWebService.php")
    return _send


def test_mint_405_holds_off_further_mint_attempts(monkeypatch):
    sess = S.Session(scope="highcourt")
    calls = []
    monkeypatch.setattr(sess, "_send", _throttled_send(calls))

    with pytest.raises(RateLimited):
        sess.init()            # first mint attempt reaches the endpoint, 405s
    with pytest.raises(RateLimited):
        sess._ensure_jwt()     # real call path: must NOT re-hit the mint endpoint
    with pytest.raises(RateLimited):
        sess.init()

    assert calls == ["appReleaseWebService.php"], (
        "cooldown must stop repeated mint attempts flooding the bootstrap endpoint"
    )


def test_mint_retried_after_cooldown_window(monkeypatch):
    sess = S.Session(scope="highcourt")
    calls = []
    monkeypatch.setattr(sess, "_send", _throttled_send(calls))
    clock = [1000.0]
    monkeypatch.setattr(S.time, "monotonic", lambda: clock[0])

    with pytest.raises(RateLimited):
        sess.init()
    clock[0] += S._MINT_COOLDOWN_SECONDS + 1.0   # window elapses
    with pytest.raises(RateLimited):
        sess.init()

    assert len(calls) == 2, "after the cooldown elapses, the mint must be retried"


def test_successful_mint_is_unaffected(monkeypatch):
    sess = S.Session(scope="highcourt")
    monkeypatch.setattr(
        sess, "_send",
        lambda endpoint, payload, *, with_bearer: {"token": "jwt-xyz"},
    )
    sess.init()
    assert sess.jwt == "jwt-xyz"
