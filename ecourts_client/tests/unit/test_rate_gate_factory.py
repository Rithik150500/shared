from __future__ import annotations

import ecourts_client._session as s
from ecourts_client._session import _RateGate, _get_rate_gate
from ecourts_client.resilience.redis_limiter import RedisRateLimiter


def _reset(monkeypatch):
    monkeypatch.setattr(s, "_rate_gate", None)


def test_flag_off_returns_threading_rate_gate(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("ECOURTS_USE_REDIS_LIMITER", "false")
    assert isinstance(_get_rate_gate(), _RateGate)


def test_flag_on_returns_redis_limiter(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("ECOURTS_USE_REDIS_LIMITER", "true")
    monkeypatch.setenv("ECOURTS_MIN_REQUEST_INTERVAL_SECONDS", "0.5")
    gate = _get_rate_gate()
    assert isinstance(gate, RedisRateLimiter)
    assert gate.base_interval == 0.5  # seeds the limiter base from the Tier-1 interval
