"""Regressions for defects found by adversarial review of the per-court breaker.

Each test maps to a numbered finding and was written to fail against the
implementation as first committed (6e5c10f).
"""
from __future__ import annotations

import pytest

from ecourts_client.errors import CircuitOpen, CourtSiteDown, RateLimited
from ecourts_client.resilience.circuit_breaker import (
    CircuitBreaker,
    _CircuitRegistry,
    with_circuit_breaker_sync,
)
from ecourts_client.resilience.court_key import GLOBAL_KEY, UNKNOWN_KEY, court_key_for_cnr
from ecourts_client.resilience.per_court import PerCourtPolicy

DL = "DLHC010012342023"   # hc:DL
RJ = "RJAU019999992015"   # dc:RJ
KA = "KAAU019999992015"   # dc:KA
MH = "MHAU019999992015"   # dc:MH
UP = "UPAU019999992015"   # dc:UP


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# --------------------------------------------------------------- finding 1
# LIVE IN PRODUCTION, independent of both flags.

def test_stale_inflight_success_cannot_reclose_an_open_breaker():
    """A call admitted BEFORE the breaker opened must not re-close it on return.

    The semaphore wraps outside the breaker (max_concurrency=10), so ~10 calls
    are always in flight past the gate. Without generation fencing the next
    healthy response re-closes an open breaker and it never latches -- it flaps.
    """
    clk = FakeClock()
    cb = CircuitBreaker(name="fence1", failure_threshold=2,
                        recovery_timeout=60.0, clock=clk)

    allowed, gen = cb.try_acquire()          # in-flight call is admitted
    assert allowed is True

    cb.record_failure()                      # meanwhile other calls open it
    cb.record_failure()
    assert cb.state == "open"

    cb.record_success(gen)                   # the stale call returns OK
    assert cb.state == "open", "a stale success must not re-close the breaker"
    assert cb.allow_request() is False


def test_stale_inflight_failure_does_not_double_count():
    clk = FakeClock()
    cb = CircuitBreaker(name="fence2", failure_threshold=2,
                        recovery_timeout=60.0, clock=clk)
    _, gen = cb.try_acquire()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    before = cb.time_until_retry()
    cb.record_failure(gen)                   # stale failure from the old epoch
    assert cb.time_until_retry() <= before   # must not escalate the ladder


def test_fresh_success_still_closes_a_half_open_breaker():
    """Fencing must not break legitimate recovery."""
    clk = FakeClock()
    cb = CircuitBreaker(name="fence3", failure_threshold=1,
                        recovery_timeout=60.0, clock=clk)
    cb.record_failure()
    assert cb.state == "open"
    clk.advance(61)
    allowed, gen = cb.try_acquire()          # half-open probe
    assert allowed is True
    cb.record_success(gen)
    assert cb.state == "closed"


# --------------------------------------------------------------- finding 2

def test_unknown_key_falls_back_to_the_global_breaker():
    """UNKNOWN_KEY must NOT get court semantics.

    Hint-less fetch_pdf keys to UNKNOWN_KEY. Giving it a windowed court breaker
    (where successes deliberately don't heal) made the change LESS protective
    than today: scattered socket errors opened a bucket that stalled every
    hint-less PDF fetch.
    """
    _CircuitRegistry.reset()
    policy = PerCourtPolicy(key_fn=lambda _x: UNKNOWN_KEY, failure_threshold=3,
                            recovery_timeout=120.0, failure_window_seconds=300.0,
                            cascade_open_threshold=0)

    @with_circuit_breaker_sync(name=GLOBAL_KEY, failure_threshold=99,
                               recovery_timeout=60.0, use_taxonomy=True, per_court=policy)
    def call(x):
        raise CourtSiteDown("socket")

    for _ in range(5):
        with pytest.raises(CourtSiteDown):
            call("whatever")

    names = {n for n, _ in _CircuitRegistry.all_items()}
    assert UNKNOWN_KEY not in names, "unknown key must not mint a windowed breaker"


# --------------------------------------------------------------- finding 3

def test_cascade_guard_counts_courts_that_have_aged_into_half_open():
    """The census must not undercount as courts age out of `open`.

    Reading the mutating `.state` transitioned open -> half_open, so the count
    saturated (~2) and the guard was inert for any staggered outage.
    """
    _CircuitRegistry.reset()
    clk = FakeClock()
    policy = PerCourtPolicy(key_fn=court_key_for_cnr, failure_threshold=2,
                            recovery_timeout=120.0, failure_window_seconds=300.0,
                            cascade_open_threshold=3, clock=clk)

    @with_circuit_breaker_sync(name=GLOBAL_KEY, failure_threshold=99,
                               recovery_timeout=60.0, use_taxonomy=True, per_court=policy)
    def call(cnr):
        raise CourtSiteDown("502")

    # Stagger the onset so earlier courts age past their 120s recovery.
    for cnr in (DL, RJ, MH):
        for _ in range(2):
            with pytest.raises(CourtSiteDown):
                call(cnr)
        clk.advance(130)

    assert _CircuitRegistry.get(GLOBAL_KEY).state == "open", (
        "cascade guard must still fire when courts have aged into half_open"
    )


def test_cascade_census_does_not_mutate_breaker_state():
    _CircuitRegistry.reset()
    clk = FakeClock()
    cb = _CircuitRegistry.get("dc:MH", failure_threshold=1, recovery_timeout=60.0,
                              failure_window_seconds=300.0, clock=clk)
    cb.record_failure()
    assert cb.state == "open"
    clk.advance(61)
    from ecourts_client.resilience.circuit_breaker import _count_tripped_courts
    _count_tripped_courts()
    # The census must not have handed out the half-open probe token.
    assert cb.peek_state() == "half_open"
    assert cb.allow_request() is True, "census must not have consumed the probe"


# --------------------------------------------------------------- finding 4

def test_a_down_court_does_not_burn_the_global_half_open_probe():
    """An open court must not steal the global breaker's recovery probe."""
    _CircuitRegistry.reset()
    clk = FakeClock()
    # Pre-register the GLOBAL breaker on the same fake clock -- the decorator
    # would otherwise build it with the real time.monotonic and the advance
    # below would never reach it. get() returns the existing instance.
    _CircuitRegistry.get(GLOBAL_KEY, failure_threshold=1,
                         recovery_timeout=60.0, clock=clk)
    policy = PerCourtPolicy(key_fn=court_key_for_cnr, failure_threshold=1,
                            recovery_timeout=600.0, failure_window_seconds=300.0,
                            cascade_open_threshold=0, clock=clk)

    @with_circuit_breaker_sync(name=GLOBAL_KEY, failure_threshold=1,
                               recovery_timeout=60.0, use_taxonomy=True, per_court=policy)
    def call(cnr):
        if cnr == DL:
            raise CourtSiteDown("502")
        if cnr == KA:
            raise RateLimited("429")
        return "ok"

    with pytest.raises(CourtSiteDown):
        call(DL)                       # opens hc:DL
    with pytest.raises(RateLimited):
        call(KA)                       # opens the global
    clk.advance(61)                    # global becomes half-open

    with pytest.raises(CircuitOpen):
        call(DL)                       # rejected by the COURT gate...
    # ...and the global's probe must still be available for a healthy court.
    assert call(RJ) == "ok"


# --------------------------------------------------------------- finding 6

def test_circuit_open_reports_the_court_name_and_a_real_retry_after():
    _CircuitRegistry.reset()
    clk = FakeClock()
    policy = PerCourtPolicy(key_fn=court_key_for_cnr, failure_threshold=1,
                            recovery_timeout=120.0, failure_window_seconds=300.0,
                            cascade_open_threshold=0, clock=clk)

    @with_circuit_breaker_sync(name=GLOBAL_KEY, failure_threshold=99,
                               recovery_timeout=60.0, use_taxonomy=True, per_court=policy)
    def call(cnr):
        raise CourtSiteDown("502")

    with pytest.raises(CourtSiteDown):
        call(DL)
    with pytest.raises(CircuitOpen) as ei:
        call(DL)
    assert ei.value.name == "hc:DL"
    assert ei.value.retry_after_seconds == pytest.approx(120.0, abs=1.0)


def test_time_until_retry_reports_the_regrant_delay_when_half_open_without_token():
    clk = FakeClock()
    cb = CircuitBreaker(name="tur", failure_threshold=1, recovery_timeout=60.0, clock=clk)
    cb.record_failure()
    clk.advance(61)
    assert cb.allow_request() is True      # consumes the probe
    assert cb.allow_request() is False     # no token left
    assert cb.time_until_retry() > 0.0, "must not advertise retry-immediately"


def test_time_until_retry_never_understates_the_escalated_ladder():
    """This number is rendered verbatim to end users at routers/cases.py:385-388.

    The half-open-without-token branch reported the BASE recovery (60s) even once
    failed probes had escalated the ladder to 480s -- so a user mid-outage was
    told "try again in about 1 minute" during an 8-minute wait, inviting exactly
    the premature retry the comment at cases.py:384 warns against. Observed on
    prod 2026-07-29: a real user was shown "about 7 minutes" only because they
    happened to hit the *open* branch; the half-open branch would have lied.
    """
    clk = FakeClock()
    cb = CircuitBreaker(name="tur_ladder", failure_threshold=1,
                        recovery_timeout=60.0, clock=clk)
    cb.record_failure()                        # closed -> open, ladder at base

    for _ in range(3):                         # 60 -> 120 -> 240 -> 480
        clk.advance(cb.time_until_retry() + 1)
        assert cb.allow_request() is True      # this caller wins the probe...
        cb.record_failure()                    # ...and it fails, doubling the ladder

    clk.advance(cb.time_until_retry() + 1)     # ladder elapses, token re-arms
    assert cb.allow_request() is True          # probe consumed
    assert cb.allow_request() is False         # half_open, no token left

    assert cb.time_until_retry() >= 480.0 * 0.99, (
        "must not advertise the 60s base while the ladder is actually at 480s"
    )


# ------------------------------------------------ mutation-surfaced gaps
# Each of these was written because a mutant SURVIVED the suite.

@pytest.mark.asyncio
async def test_async_decorator_also_reports_the_court_name():
    """The async wrapper is a near-copy of the sync one and is the PRODUCTION
    path; mutating only its CircuitOpen raise site survived the whole suite."""
    from ecourts_client.resilience.circuit_breaker import with_circuit_breaker

    _CircuitRegistry.reset()
    clk = FakeClock()
    policy = PerCourtPolicy(key_fn=court_key_for_cnr, failure_threshold=1,
                            recovery_timeout=120.0, failure_window_seconds=300.0,
                            cascade_open_threshold=0, clock=clk)

    @with_circuit_breaker(name=GLOBAL_KEY, failure_threshold=99,
                          recovery_timeout=60.0, use_taxonomy=True, per_court=policy)
    async def call(cnr):
        raise CourtSiteDown("502")

    with pytest.raises(CourtSiteDown):
        await call(DL)
    with pytest.raises(CircuitOpen) as ei:
        await call(DL)
    assert ei.value.name == "hc:DL"
    assert ei.value.retry_after_seconds == pytest.approx(120.0, abs=1.0)


def test_a_recovered_court_closes_via_a_successful_probe():
    """Nothing asserted that court successes are recorded, so deleting
    `court_cb.record_success(...)` survived -- meaning a recovered court would
    stay open until process restart."""
    _CircuitRegistry.reset()
    clk = FakeClock()
    policy = PerCourtPolicy(key_fn=court_key_for_cnr, failure_threshold=1,
                            recovery_timeout=120.0, failure_window_seconds=300.0,
                            cascade_open_threshold=0, clock=clk)
    healthy = {"yes": False}

    @with_circuit_breaker_sync(name=GLOBAL_KEY, failure_threshold=99,
                               recovery_timeout=60.0, use_taxonomy=True, per_court=policy)
    def call(cnr):
        if healthy["yes"]:
            return "ok"
        raise CourtSiteDown("502")

    with pytest.raises(CourtSiteDown):
        call(DL)
    assert _CircuitRegistry.get("dc:MH") is not None  # registry sanity
    court = dict(_CircuitRegistry.all_items())["hc:DL"]
    assert court.peek_state() == "open"

    clk.advance(121)            # court ages into half_open
    healthy["yes"] = True
    assert call(DL) == "ok"     # successful probe
    assert court.peek_state() == "closed", "a recovered court must close"


def test_court_probe_is_returned_when_the_global_gate_rejects():
    """Court allows, global rejects -> the court's half-open token must be
    credited back. Deleting return_probe() survived because the existing test
    rejects at the COURT gate and never reaches this path."""
    _CircuitRegistry.reset()
    clk = FakeClock()
    # Pre-register the global on the fake clock (side effect only).
    _CircuitRegistry.get(GLOBAL_KEY, failure_threshold=1,
                         recovery_timeout=600.0, clock=clk)
    policy = PerCourtPolicy(key_fn=court_key_for_cnr, failure_threshold=1,
                            recovery_timeout=60.0, failure_window_seconds=300.0,
                            cascade_open_threshold=0, clock=clk)

    @with_circuit_breaker_sync(name=GLOBAL_KEY, failure_threshold=1,
                               recovery_timeout=600.0, use_taxonomy=True, per_court=policy)
    def call(cnr):
        if cnr == KA:
            raise RateLimited("429")
        raise CourtSiteDown("502")

    with pytest.raises(CourtSiteDown):
        call(DL)                       # opens hc:DL (60s recovery)
    with pytest.raises(RateLimited):
        call(KA)                       # opens the global (600s recovery)
    court = dict(_CircuitRegistry.all_items())["hc:DL"]

    clk.advance(61)                    # hc:DL half-open with a token; global still open
    with pytest.raises(CircuitOpen) as ei:
        call(DL)
    assert ei.value.name == GLOBAL_KEY, "should be rejected by the GLOBAL gate"
    assert court.allow_request() is True, "court probe must have been returned"
