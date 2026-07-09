"""_RateGate paces outbound eCourts calls to a minimum interval so bulk
add/refresh bursts can't trip eCourts' 405/HTML IP throttle (docs/RE_NOTES_v4.md).

The gate's ``monotonic``/``sleep`` are injectable so we can assert the exact slot
arithmetic deterministically -- no real sleeping.
"""
from __future__ import annotations

from ecourts_client._session import _RateGate


class _FakeClock:
    """A controllable monotonic clock whose ``sleep`` advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def test_first_call_does_not_wait():
    clk = _FakeClock()
    g = _RateGate(0.5, monotonic=clk.monotonic, sleep=clk.sleep)
    g.wait()
    assert clk.sleeps == []


def test_back_to_back_calls_are_spaced_by_the_interval():
    clk = _FakeClock()
    g = _RateGate(0.5, monotonic=clk.monotonic, sleep=clk.sleep)
    g.wait()  # slot 0.0, no sleep
    g.wait()  # slot 0.5, sleep 0.5 -> now 0.5
    g.wait()  # slot 1.0, sleep 0.5 -> now 1.0
    assert clk.sleeps == [0.5, 0.5]
    assert clk.now == 1.0


def test_caller_that_arrives_after_its_slot_does_not_wait():
    clk = _FakeClock()
    g = _RateGate(0.5, monotonic=clk.monotonic, sleep=clk.sleep)
    g.wait()          # reserves 0.0, next_allowed = 0.5
    clk.now = 2.0     # plenty of idle time passes before the next call
    g.wait()          # slot = max(2.0, 0.5) = 2.0, no sleep
    assert clk.sleeps == []


def test_zero_interval_disables_the_gate():
    clk = _FakeClock()
    g = _RateGate(0.0, monotonic=clk.monotonic, sleep=clk.sleep)
    for _ in range(5):
        g.wait()
    assert clk.sleeps == []


def test_negative_interval_is_treated_as_disabled():
    clk = _FakeClock()
    g = _RateGate(-1.0, monotonic=clk.monotonic, sleep=clk.sleep)
    g.wait()
    g.wait()
    assert clk.sleeps == []
