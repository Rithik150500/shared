"""The half-open backoff ladder: bound it, and stop phase-locking to pollers.

Two independent defects, both observed on prod 2026-07-29:

1. UNBOUNDED LADDER. ``record_failure`` doubles ``_current_recovery_timeout``
   per failed probe and ``_probe_failures`` is monotonic -- reset ONLY by a
   *successful* half-open probe. The ``max_recovery_timeout`` ceiling exists as a
   constructor argument but is not plumbed through ``with_circuit_breaker``,
   ``with_circuit_breaker_sync``, ``_CircuitRegistry.get``, ``_court_breaker`` or
   ``PerCourtPolicy``, so production could never set it: the effective ceiling
   was the hardcoded 1800s. A court that recovers at minute 2 waits 30.

2. PHASE LOCK. The ladder is deterministic and the token is a single bool with
   no jitter, so a caller on a fixed cadence lands on the re-arm instant every
   time. Prod: a ``*/20`` cron retrying at exactly 60s intervals won the probe on
   every rung, walking the circuit 60 -> 120 -> 240 -> 480s and keeping it open
   for everyone else. The same determinism makes N court breakers that open
   together re-probe in lockstep (thundering herd).

These are separate: a cap alone still lets a poller win every probe; jitter alone
still lets the ladder reach the ceiling. Both are needed.
"""
from __future__ import annotations

import pytest

from ecourts_client.resilience.circuit_breaker import (
    CircuitBreaker,
    _CircuitRegistry,
    with_circuit_breaker_sync,
)
from ecourts_client.resilience.court_key import court_key_for_cnr
from ecourts_client.resilience.per_court import PerCourtPolicy


class FakeClock:
    """Injectable monotonic clock. Patching ``time.monotonic`` globally is unsafe
    -- asyncio shares it."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _drive_failed_probe(cb, clk) -> None:
    """One full open -> half_open -> failed-probe -> open cycle, arriving exactly
    on the re-arm instant the way a fixed-cadence poller does."""
    clk.advance(cb.time_until_retry() + 1)
    assert cb.allow_request() is True, "should have won the half-open probe"
    cb.record_failure()


# --- plumbing: the ceiling must be reachable from production ---------------

def test_sync_decorator_accepts_max_recovery_timeout():
    """RED today: TypeError. The constructor parameter exists but no decorator,
    registry call, or config field can reach it -- so the 1800s default was
    effectively hardcoded in production."""
    _CircuitRegistry.reset()

    @with_circuit_breaker_sync(
        name="plumb_sync", failure_threshold=1,
        recovery_timeout=60.0, max_recovery_timeout=300.0,
    )
    def call():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        call()
    assert _CircuitRegistry.get("plumb_sync")._max_recovery_timeout == 300.0


def test_async_decorator_accepts_max_recovery_timeout():
    """The async wrapper is the PRODUCTION path and is a hand-copy of the sync
    one; past mutation testing showed a defect injected into only one of them
    survived the whole suite."""
    from ecourts_client.resilience.circuit_breaker import with_circuit_breaker

    _CircuitRegistry.reset()

    @with_circuit_breaker(
        name="plumb_async", failure_threshold=1,
        recovery_timeout=60.0, max_recovery_timeout=300.0,
    )
    async def call():
        raise RuntimeError("boom")

    import asyncio

    with pytest.raises(RuntimeError):
        asyncio.run(call())
    assert _CircuitRegistry.get("plumb_async")._max_recovery_timeout == 300.0


def test_per_court_policy_forwards_max_recovery_timeout():
    """Court breakers start at a 120s base, so they need their own ceiling."""
    _CircuitRegistry.reset()
    policy = PerCourtPolicy(
        key_fn=court_key_for_cnr, failure_threshold=1,
        recovery_timeout=120.0, failure_window_seconds=300.0,
        cascade_open_threshold=0, max_recovery_timeout=600.0,
    )
    assert policy.max_recovery_timeout == 600.0


# --- the ladder itself -----------------------------------------------------

def test_ladder_saturates_at_the_configured_ceiling():
    """Guard, not red: the ceiling already works when it can be set. What was
    broken is that production could not set it (see the plumbing tests)."""
    clk = FakeClock()
    cb = CircuitBreaker(name="cap", failure_threshold=1, recovery_timeout=60.0,
                        max_recovery_timeout=300.0, clock=clk)
    cb.record_failure()
    for _ in range(10):
        _drive_failed_probe(cb, clk)
    assert cb.time_until_retry() <= 300.0 + 1e-6


def test_probe_failure_counter_is_bounded():
    """RED today. ``2 ** _probe_failures`` on a monotonic counter is an unbounded
    bignum computed while holding the lock. The ``min()`` against any sane ceiling
    saturated long ago, so every exponent past ~16 is pure waste."""
    clk = FakeClock()
    cb = CircuitBreaker(name="bignum", failure_threshold=1, recovery_timeout=60.0,
                        max_recovery_timeout=300.0, clock=clk)
    cb.record_failure()
    for _ in range(200):
        _drive_failed_probe(cb, clk)
    assert cb._probe_failures <= 16, (
        f"probe counter reached {cb._probe_failures}; it must be bounded"
    )


# --- jitter ----------------------------------------------------------------

def test_jitter_defaults_off():
    """PARITY GUARD -- load-bearing. Every existing test that constructs a
    CircuitBreaker directly relies on the exact 60/120/240 ladder. Defaulting
    jitter to 0.0 is what lets those files stay untouched."""
    clk = FakeClock()
    cb = CircuitBreaker(name="nojitter", failure_threshold=1,
                        recovery_timeout=60.0, clock=clk)
    cb.record_failure()
    assert cb.time_until_retry() == pytest.approx(60.0, abs=0.01)
    for expected in (120.0, 240.0):
        _drive_failed_probe(cb, clk)
        assert cb.time_until_retry() == pytest.approx(expected, abs=0.01)


def test_jitter_is_downward_only_and_bounded():
    """RED today. Downward-only, chosen deliberately over symmetric +/-: it can
    never exceed the documented ceiling (so the cap stays a hard cap) and can
    never lengthen a user's wait beyond the nominal ladder."""
    clk = FakeClock()
    worst = CircuitBreaker(name="j_full", failure_threshold=1, recovery_timeout=60.0,
                           jitter=0.2, rng=lambda: 1.0, clock=clk)
    worst.record_failure()
    assert worst.time_until_retry() == pytest.approx(48.0, abs=0.01)

    none = CircuitBreaker(name="j_zero", failure_threshold=1, recovery_timeout=60.0,
                          jitter=0.2, rng=lambda: 0.0, clock=FakeClock())
    none.record_failure()
    assert none.time_until_retry() == pytest.approx(60.0, abs=0.01)


def test_jitter_decorrelates_breakers_that_open_together():
    """RED today. THE PHASE-LOCK PROPERTY.

    With a deterministic ladder every breaker that opens at the same instant
    re-arms at the same instant, and a fixed-cadence caller lands on that instant
    every time. Jitter is what makes the re-arm moment unpredictable, so the
    poller stops winning the probe on every rung and a thundering herd of court
    breakers stops re-probing in lockstep.
    """
    import random

    clk = FakeClock()
    rng = random.Random(1234)

    def make(name, jitter):
        cb = CircuitBreaker(name=name, failure_threshold=1, recovery_timeout=60.0,
                            jitter=jitter, rng=rng.random, clock=clk)
        cb.record_failure()
        return cb.time_until_retry()

    locked = {make(f"lock{i}", 0.0) for i in range(8)}
    assert len(locked) == 1, "without jitter, breakers opening together are in lockstep"

    spread = {make(f"spread{i}", 0.2) for i in range(8)}
    assert len(spread) > 1, "jitter must decorrelate simultaneous opens"
    assert max(spread) <= 60.0 + 1e-6, "jitter is downward-only; never past nominal"
    assert min(spread) >= 48.0 - 1e-6, "and bounded by the jitter fraction"


def test_successful_probe_still_resets_the_ladder():
    """PARITY GUARD. A recovering court must fall straight back to base, not
    inherit a climbed ladder."""
    clk = FakeClock()
    cb = CircuitBreaker(name="reset", failure_threshold=1, recovery_timeout=60.0, clock=clk)
    cb.record_failure()
    _drive_failed_probe(cb, clk)
    _drive_failed_probe(cb, clk)
    assert cb.time_until_retry() > 60.0

    clk.advance(cb.time_until_retry() + 1)
    assert cb.allow_request() is True
    cb.record_success()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.time_until_retry() == pytest.approx(60.0, abs=0.01)
