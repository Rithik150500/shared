"""Per-court circuit breakers: isolation, global hostility, and the cascade guard.

Today one process-wide `ecourts_global` breaker fronts every court, so a real
availability failure on Delhi HC blocks Rajasthan district and every picker
endpoint. Keying the breaker by court contains that -- but naive per-court
keying makes a BROAD outage worse (N independent half-open ladders probing an
IP that bans on burst), so the cascade guard collapses many open courts back
onto the global breaker.
"""
from __future__ import annotations

import pytest

from ecourts_client.errors import CircuitOpen, CNRNotFound, CourtSiteDown, RateLimited
from ecourts_client.resilience.circuit_breaker import (
    _CircuitRegistry,
    with_circuit_breaker_sync,
)
from ecourts_client.resilience.court_key import GLOBAL_KEY, court_key_for_cnr
from ecourts_client.resilience.per_court import PerCourtPolicy

MH = "MHAU019999992015"   # dc:MH
DL = "DLHC010012342023"   # hc:DL
RJ = "RJAU019999992015"   # dc:RJ
KA = "KAAU019999992015"   # dc:KA


def _policy(**over) -> PerCourtPolicy:
    kw = dict(
        key_fn=lambda cnr: court_key_for_cnr(cnr),
        failure_threshold=3,
        recovery_timeout=120.0,
        failure_window_seconds=300.0,
        cascade_open_threshold=0,   # off unless a test asks for it
    )
    kw.update(over)
    return PerCourtPolicy(**kw)


def _wrap(policy, exc_map):
    """Build a wrapped fn that raises exc_map[cnr] (or returns 'ok')."""
    # Global threshold 3 as well: court-scoped failures route to the COURT
    # breaker, so a realistic global threshold still leaves it closed.
    @with_circuit_breaker_sync(
        name=GLOBAL_KEY, failure_threshold=3, recovery_timeout=60.0,
        use_taxonomy=True, per_court=policy,
    )
    def call(cnr):
        exc = exc_map.get(cnr)
        if exc is not None:
            raise exc
        return "ok"
    return call


def test_one_court_going_down_does_not_block_another():
    """The headline behaviour this change exists for."""
    _CircuitRegistry.reset()
    call = _wrap(_policy(), {DL: CourtSiteDown("502")})

    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            call(DL)
    # Delhi HC is now open...
    with pytest.raises(CircuitOpen):
        call(DL)
    # ...but Rajasthan district is untouched.
    assert call(RJ) == "ok"


def test_ip_wide_throttle_blocks_every_court():
    """RateLimited is a property of the egress IP, so it must gate globally."""
    _CircuitRegistry.reset()
    call = _wrap(_policy(), {MH: RateLimited("429")})

    for _ in range(3):
        with pytest.raises(RateLimited):
            call(MH)
    # A different court is now ALSO blocked -- the global breaker is open.
    with pytest.raises(CircuitOpen):
        call(RJ)


def test_bad_cnrs_open_neither_breaker():
    """Taxonomy still applies: client errors are not availability signals."""
    _CircuitRegistry.reset()
    call = _wrap(_policy(), {MH: CNRNotFound(MH)})

    for _ in range(10):
        with pytest.raises(CNRNotFound):
            call(MH)
    assert call(RJ) == "ok"
    assert _CircuitRegistry.get(GLOBAL_KEY).state == "closed"
    with pytest.raises(CNRNotFound):
        call(MH)  # still reaching the function, not short-circuited by a breaker


def test_court_failures_do_not_open_the_global_breaker():
    _CircuitRegistry.reset()
    call = _wrap(_policy(), {DL: CourtSiteDown("502")})
    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            call(DL)
    assert _CircuitRegistry.get(GLOBAL_KEY).state == "closed"


def test_cascade_guard_force_opens_global_when_many_courts_are_open():
    """A broad outage must collapse onto ONE breaker, not N probing ladders."""
    _CircuitRegistry.reset()
    down = {c: CourtSiteDown("502") for c in (MH, DL, RJ)}
    call = _wrap(_policy(cascade_open_threshold=3), down)

    for cnr in (MH, DL, RJ):
        for _ in range(3):
            with pytest.raises(CourtSiteDown):
                call(cnr)

    assert _CircuitRegistry.get(GLOBAL_KEY).state == "open"
    # An unrelated, healthy court is now blocked too -- intended: the host is
    # hostile, so the process goes quiet.
    with pytest.raises(CircuitOpen):
        call(KA)


def test_cascade_guard_does_not_fire_below_threshold():
    _CircuitRegistry.reset()
    down = {c: CourtSiteDown("502") for c in (MH, DL)}
    call = _wrap(_policy(cascade_open_threshold=3), down)
    for cnr in (MH, DL):
        for _ in range(3):
            with pytest.raises(CourtSiteDown):
                call(cnr)
    assert _CircuitRegistry.get(GLOBAL_KEY).state == "closed"
    assert call(KA) == "ok"


def test_unknown_court_keys_share_one_bucket():
    """Junk CNRs must not mint a registry entry each."""
    _CircuitRegistry.reset()
    call = _wrap(_policy(), {})
    for junk in ("nope", "", "ZZAU019999992015", None):
        call(junk)
    names = {n for n, _ in _CircuitRegistry.all_items()}
    assert not any(n.startswith(("dc:", "hc:")) for n in names)


def test_disabled_policy_keeps_todays_single_breaker_behaviour():
    """per_court=None must be byte-identical to the current code path."""
    _CircuitRegistry.reset()

    @with_circuit_breaker_sync(
        name=GLOBAL_KEY, failure_threshold=3, recovery_timeout=60.0, use_taxonomy=True,
    )
    def call(cnr):
        raise CourtSiteDown("502")

    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            call(DL)
    # No per-court keying: the GLOBAL breaker took the hit, so every court is blocked.
    with pytest.raises(CircuitOpen):
        call(RJ)
    assert not any(n.startswith(("dc:", "hc:")) for n, _ in _CircuitRegistry.all_items())
