"""Mint-flood guard for ecourts_client._session.Session.

A 405 on the JWT-mint bootstrap (appReleaseWebService.php) leaves self.jwt None,
so _ensure_jwt would re-attempt the mint on EVERY subsequent call, hammering the
most throttle-sensitive endpoint and turning a transient burst into a sustained
outage. After a mint 405/429 the session must hold off further mint attempts for
a cooldown window (callers fast-fail with RateLimited instead of re-flooding).
"""
import time

import pytest
from ecourts_client import _session as S
from ecourts_client.errors import RateLimited

REDIS_URL = "redis://localhost:6379/15"


@pytest.fixture()
def redis_env(monkeypatch):
    """Real Redis on db 15 (same convention as test_redis_limiter), skipping
    cleanly when none is reachable. Resets the module Redis singleton so the
    client is rebuilt against db 15 rather than a leaked earlier handle."""
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


def test_mint_cooldown_is_shared_across_sessions(redis_env, monkeypatch):
    """THE FLEET BUG: the cooldown was a per-instance attribute, so every fresh
    process/fork got its own window and each one was free to re-hit the mint
    endpoint. Two distinct Sessions simulate two worker processes: once ONE has
    been 405'd, the OTHER must fast-fail WITHOUT touching the bootstrap."""
    calls = []
    first = S.Session(scope="highcourt")
    second = S.Session(scope="highcourt")
    monkeypatch.setattr(first, "_send", _throttled_send(calls))
    monkeypatch.setattr(second, "_send", _throttled_send(calls))

    with pytest.raises(RateLimited):
        first.init()               # reaches the endpoint once, 405s, arms cooldown
    with pytest.raises(RateLimited):
        second.init()              # different process: must NOT re-hit the mint

    assert calls == ["appReleaseWebService.php"], (
        "the mint cooldown must be fleet-wide (Redis), not per-process -- a second "
        "process re-hitting the bootstrap is exactly the flood this guards against"
    )


def test_shared_mint_cooldown_expires_and_mint_resumes(redis_env, monkeypatch):
    """The fleet window MUST be self-clearing. A shared key written without an
    expiry would wedge every process off the mint endpoint permanently -- a far
    worse outage than the flood it guards against."""
    monkeypatch.setattr(S, "_MINT_COOLDOWN_SECONDS", 0.3)
    calls = []
    first = S.Session(scope="highcourt")
    second = S.Session(scope="highcourt")
    monkeypatch.setattr(first, "_send", _throttled_send(calls))
    monkeypatch.setattr(second, "_send", _throttled_send(calls))

    with pytest.raises(RateLimited):
        first.init()
    assert redis_env.pttl(S._mint_cooldown_key("highcourt")) > 0, "window must carry a TTL"

    time.sleep(0.4)                        # window lapses
    with pytest.raises(RateLimited):
        second.init()                      # must be allowed to try again

    assert len(calls) == 2, "once the fleet window lapses the mint must be retried"


def test_mint_cooldown_fails_open_when_redis_is_unreachable(monkeypatch):
    """Redis down must NOT break minting. The guard degrades to the per-process
    window rather than raising out of the transport path."""
    monkeypatch.setenv("SHARED_REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listening
    monkeypatch.setattr(S, "_throttle_redis_singleton", None)

    assert S._shared_mint_cooldown_remaining("highcourt") == 0.0
    S._arm_shared_mint_cooldown("highcourt")     # must not raise

    sess = S.Session(scope="highcourt")
    monkeypatch.setattr(
        sess, "_send",
        lambda endpoint, payload, *, with_bearer: {"token": "jwt-xyz"},
    )
    sess.init()
    assert sess.jwt == "jwt-xyz", "an unreachable Redis must never block a good mint"


def test_successful_mint_is_unaffected(monkeypatch):
    sess = S.Session(scope="highcourt")
    monkeypatch.setattr(
        sess, "_send",
        lambda endpoint, payload, *, with_bearer: {"token": "jwt-xyz"},
    )
    sess.init()
    assert sess.jwt == "jwt-xyz"
