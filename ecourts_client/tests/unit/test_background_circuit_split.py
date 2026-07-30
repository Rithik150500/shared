"""Background refresh failures must not lock interactive users out.

Prod 2026-07-30 11:05-11:07 UTC. A paying trial user's Add Case search:

    11:05:23  scheduler: "Refreshing 126 due cases"
    11:05:57  HER SEARCH SUCCEEDS  (audit_log: party-name / district)
    11:06:13  scheduler 405 caseHistoryWebService.php
    11:06:14  scheduler 405 caseHistoryWebService.php
    11:06:16  scheduler 405 caseHistoryWebService.php
    11:06:17  scheduler 405 listOfCasesWebService.php
              -> Circuit 'ecourts_global' open: failures=5
    11:06:46  her next search -> CircuitOpen, retry after 26.5s

``ecourts_global`` is ONE breaker shared by the 15-minute refresh scheduler and by
interactive search, so background failures gate reads the user never caused. Note her
11:05:57 search SUCCEEDED while the scheduler was already mid-poll -- interactive calls
demonstrably can get through while background ones are failing.

The split: a contextvar marks a call background; background failures record against
``<name>:bg`` and background admission consults that breaker, leaving the interactive
breaker untouched. Interactive is NOT given a bypass -- it still has its own breaker with
the same threshold, so if eCourts is genuinely refusing us the interactive side trips on
its own after 5 of ITS OWN failures. Worst case we spend ~5 extra requests during a
throttle window instead of blocking every user for the whole window.

Default is INTERACTIVE. Anything that does not opt in behaves exactly as today, which is
what makes a partial rollout safe: if only casepilot ships this, the bot is unchanged.
"""
from __future__ import annotations

import pytest

from ecourts_client.errors import CircuitOpen, RateLimited
from ecourts_client.resilience.circuit_breaker import (
    _CircuitRegistry,
    background_calls,
    with_circuit_breaker_sync,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    _CircuitRegistry.reset()
    yield
    _CircuitRegistry.reset()


def _wrapped(name="split_global", threshold=5):
    @with_circuit_breaker_sync(name=name, failure_threshold=threshold,
                               recovery_timeout=60.0, use_taxonomy=True)
    def call():
        raise RateLimited("eCourts returned 405 (burst throttle) for caseHistoryWebService.php")
    return call


def test_background_failures_do_not_open_the_interactive_circuit():
    """THE HEADLINE. Replays 11:06:13-11:06:17: the scheduler burns its five 405s, then an
    interactive caller arrives. Today she gets CircuitOpen; she must get through."""
    call = _wrapped()

    with background_calls():
        for _ in range(5):
            with pytest.raises(RateLimited):
                call()

    # Background side is now open and correctly refusing background work.
    with background_calls():
        with pytest.raises(CircuitOpen):
            call()

    # Interactive must NOT be gated by that. It reaches the transport -- and here the
    # transport still 405s, so we assert we got the REAL upstream error, not CircuitOpen.
    with pytest.raises(RateLimited):
        call()


def test_interactive_still_trips_on_its_own_failures():
    """The split must not become a bypass. If eCourts is genuinely refusing interactive
    calls too, the interactive breaker opens on its own -- bounded at `threshold` extra
    requests, not unlimited."""
    call = _wrapped()
    for _ in range(5):
        with pytest.raises(RateLimited):
            call()
    with pytest.raises(CircuitOpen):
        call()


def test_interactive_failures_do_not_open_the_background_circuit():
    """Symmetry check: the two sides are genuinely independent, so a burst of user searches
    cannot starve the refresh loop either."""
    call = _wrapped()
    for _ in range(5):
        with pytest.raises(RateLimited):
            call()
    with pytest.raises(CircuitOpen):
        call()

    with background_calls():
        with pytest.raises(RateLimited):
            call()


def test_default_is_interactive():
    """Anything that does not opt in keeps today's behaviour. This is what makes a partial
    rollout safe -- the bot can lag casepilot with no change in its semantics."""
    call = _wrapped()
    for _ in range(5):
        with pytest.raises(RateLimited):
            call()
    assert _CircuitRegistry.get("split_global").peek_state() == "open"
    assert "split_global:bg" not in dict(_CircuitRegistry.all_items())


def test_background_marker_is_scoped_and_restores():
    """The contextvar must not leak past the with-block, or the scheduler would silently
    reclassify every later request in the same task."""
    call = _wrapped()
    with background_calls():
        with pytest.raises(RateLimited):
            call()
    for _ in range(5):
        with pytest.raises(RateLimited):
            call()
    # 5 interactive failures opened the interactive side...
    assert _CircuitRegistry.get("split_global").peek_state() == "open"
    # ...and the single background failure did not.
    assert _CircuitRegistry.get("split_global:bg").peek_state() == "closed"


def test_split_can_be_disabled_by_env(monkeypatch):
    """Kill switch. With the split off, background failures gate everyone again -- exactly
    today's behaviour -- so a rollback needs no redeploy."""
    import ecourts_client.resilience.circuit_breaker as cb_mod
    monkeypatch.setattr(cb_mod, "_SPLIT_BG_CIRCUIT", False)

    call = _wrapped(name="split_off")
    with background_calls():
        for _ in range(5):
            with pytest.raises(RateLimited):
                call()
    with pytest.raises(CircuitOpen):
        call()
