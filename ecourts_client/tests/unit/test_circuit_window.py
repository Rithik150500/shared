"""Sliding-failure-window mode and force_open on CircuitBreaker.

Why a window: the legacy breaker counts CONSECUTIVE failures (record_success
zeroes the count). A state-level court key sees an interleaved success/failure
stream during a partial outage, which can never accumulate N in a row -- so a
per-court breaker with consecutive semantics would simply never trip. The
global breaker keeps consecutive semantics, byte-identical to today.

Time is INJECTED rather than monkeypatched: circuit_breaker shares
time.monotonic with asyncio, so patching it globally is unsafe.
"""
from __future__ import annotations

from ecourts_client.resilience.circuit_breaker import CircuitBreaker


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_windowed_breaker_accumulates_across_interleaved_successes():
    """The whole point: successes must not erase failures inside the window."""
    clk = FakeClock()
    cb = CircuitBreaker(name="win_interleaved", failure_threshold=3,
                        recovery_timeout=60.0, failure_window_seconds=300.0, clock=clk)
    for _ in range(2):
        cb.record_failure()
        cb.record_success()
    cb.record_failure()
    assert cb.state == "open"


def test_consecutive_breaker_is_still_reset_by_success():
    """Default (no window) keeps today's semantics exactly."""
    clk = FakeClock()
    cb = CircuitBreaker(name="win_legacy", failure_threshold=3,
                        recovery_timeout=60.0, clock=clk)
    for _ in range(2):
        cb.record_failure()
        cb.record_success()
    cb.record_failure()
    assert cb.state == "closed"


def test_failures_older_than_the_window_age_out():
    clk = FakeClock()
    cb = CircuitBreaker(name="win_expiry", failure_threshold=3,
                        recovery_timeout=60.0, failure_window_seconds=300.0, clock=clk)
    cb.record_failure()
    cb.record_failure()
    clk.advance(301)
    cb.record_failure()
    assert cb.state == "closed"  # only one failure remains inside the window


def test_successful_half_open_probe_clears_the_window():
    """Otherwise the stale failures would re-open the breaker instantly."""
    clk = FakeClock()
    cb = CircuitBreaker(name="win_recover", failure_threshold=3,
                        recovery_timeout=60.0, failure_window_seconds=300.0, clock=clk)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open"

    clk.advance(61)                 # open -> half_open
    assert cb.allow_request() is True
    cb.record_success()             # probe succeeds
    assert cb.state == "closed"

    cb.record_failure()             # a single fresh failure must not re-open
    assert cb.state == "closed"


def test_force_open_opens_immediately_regardless_of_count():
    clk = FakeClock()
    cb = CircuitBreaker(name="win_force", failure_threshold=99,
                        recovery_timeout=60.0, clock=clk)
    assert cb.allow_request() is True
    cb.force_open()
    assert cb.state == "open"
    assert cb.allow_request() is False


def test_force_open_is_idempotent_and_recovers_normally():
    clk = FakeClock()
    cb = CircuitBreaker(name="win_force2", failure_threshold=99,
                        recovery_timeout=60.0, clock=clk)
    cb.force_open()
    cb.force_open()
    assert cb.state == "open"
    clk.advance(61)
    assert cb.state == "half_open"
