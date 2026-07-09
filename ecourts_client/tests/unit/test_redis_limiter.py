"""Distributed GCRA limiter — tested against a REAL Redis (fakeredis's Lua/TIME
fidelity is insufficient for the atomicity this depends on). Uses Redis DB 15 and
flushes it per test. Skips cleanly if no Redis is reachable.

Spacing is asserted via _reserve_slot_ms() (the RAW per-call delay in ms) and by
reading the shared keys — NOT via a recorded-sleep list: the limiter only sleeps
when delay>0 (a ~0 first delay is never recorded), and a stubbed sleep does not
advance the clock, so index-based sleep assertions are unreliable.
"""
from __future__ import annotations

import threading

import pytest

from ecourts_client.resilience.redis_limiter import RedisRateLimiter

REDIS_URL = "redis://localhost:6379/15"
NEXT_KEY = "ecourts:egress:v1:next_allowed"
INTERVAL_KEY = "ecourts:egress:v1:interval"


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
    yield client
    client.flushdb()


def _limiter(**kw):
    return RedisRateLimiter(
        base_interval=kw.get("base_interval", 0.5),
        widen_factor=kw.get("widen_factor", 2.0),
        max_interval=kw.get("max_interval", 8.0),
        penalty_ttl_seconds=kw.get("penalty_ttl_seconds", 300),
        sleep=lambda d: None,
    )


def test_gcra_consecutive_reservations_space_by_interval(redis_env):
    """Immediate consecutive reservations return delays ~[0, interval, 2*interval]
    (each caller waits for its own slot). Clock-independent: asserts the returned
    delay, not a recorded sleep."""
    lim = _limiter(base_interval=0.5)
    d0, d1, d2 = lim._reserve_slot_ms(), lim._reserve_slot_ms(), lim._reserve_slot_ms()
    assert d0 <= 30
    assert 470 <= d1 <= 530
    assert 970 <= d2 <= 1030


def test_two_limiters_share_one_schedule(redis_env):
    """Distinct instances (simulating 2 processes) draw from the SAME shared slot
    schedule — the whole point of Tier-2."""
    a, b = _limiter(base_interval=0.5), _limiter(base_interval=0.5)
    delays = [a._reserve_slot_ms(), b._reserve_slot_ms(), a._reserve_slot_ms()]
    assert delays[0] <= 30
    assert 470 <= delays[1] <= 530
    assert 970 <= delays[2] <= 1030


def test_penalize_widens_shared_interval(redis_env):
    lim = _limiter(base_interval=0.5, widen_factor=2.0, max_interval=8.0)
    lim.penalize()          # 0.5 -> 1.0
    lim.penalize()          # 1.0 -> 2.0
    assert float(redis_env.get(INTERVAL_KEY)) == pytest.approx(2.0)
    lim._reserve_slot_ms()  # slot A; advances next_allowed by the widened 2.0s
    d = lim._reserve_slot_ms()   # slot B, ~2.0s after A
    assert 1900 <= d <= 2100


def test_penalize_caps_at_max(redis_env):
    lim = _limiter(base_interval=0.5, widen_factor=10.0, max_interval=3.0)
    lim.penalize(); lim.penalize()
    assert float(redis_env.get(INTERVAL_KEY)) == pytest.approx(3.0)


def test_interval_key_has_penalty_ttl_for_auto_reset(redis_env):
    lim = _limiter(base_interval=0.5, penalty_ttl_seconds=300)
    lim.penalize()
    ttl = redis_env.ttl(INTERVAL_KEY)
    assert 0 < ttl <= 300   # key expires -> GET nil -> base interval again


def test_fail_open_to_local_gate_when_redis_absent(monkeypatch):
    """No SHARED_REDIS_URL/REDIS_URL -> lazy client None -> wait() uses the
    per-process _RateGate floor and never raises. penalize() is a silent no-op.
    The floor's first slot is ~0 (not slept); the second is spaced ~base, recorded
    exactly once."""
    monkeypatch.delenv("SHARED_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    slept = []
    lim = RedisRateLimiter(base_interval=0.3, widen_factor=2.0, max_interval=8.0,
                           penalty_ttl_seconds=300, sleep=lambda d: slept.append(d))
    lim.wait()              # first slot ~0 -> _RateGate does not sleep (delay<=0)
    lim.wait()              # spaced -> _RateGate sleeps ~0.3 exactly once
    assert len(slept) == 1 and 0.25 <= slept[0] <= 0.35
    lim.penalize()         # must not raise


def test_concurrent_reservations_are_atomic(redis_env):
    """20 threads reserve 20 slots; the shared next_allowed advances by EXACTLY
    ~20*interval (no double-spend). Clock-independent: reads the key vs the server
    clock, not per-thread delays (which can collide in the same ms)."""
    lim = RedisRateLimiter(base_interval=0.1, widen_factor=2.0, max_interval=8.0,
                           penalty_ttl_seconds=300, sleep=lambda d: None)
    t0 = redis_env.time()
    start_ms = t0[0] * 1000 + t0[1] // 1000
    threads = [threading.Thread(target=lim._reserve_slot_ms) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    advanced = float(redis_env.get(NEXT_KEY)) - start_ms
    assert 1900 <= advanced <= 2300, f"20 atomic 0.1s reservations => ~2000ms advance, got {advanced}"
