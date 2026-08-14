"""STATIC_STATES is the baked-in fallback served when eCourts can't be
reached for the (quasi-immutable) state/High-Court list. Captured live
2026-07-16 from stateWebService.php (DC + HC scopes)."""
from __future__ import annotations

from ecourts_client.models import StateRef
from ecourts_client.static_data import STATIC_STATES


def test_has_both_scopes():
    assert set(STATIC_STATES) == {"district", "highcourt"}


def test_district_has_all_36_states():
    assert len(STATIC_STATES["district"]) == 36


def test_highcourt_has_all_25_high_courts():
    assert len(STATIC_STATES["highcourt"]) == 25


def test_all_entries_are_stateref():
    for scope, states in STATIC_STATES.items():
        assert all(isinstance(s, StateRef) for s in states), scope


def test_numeric_codes_unique_within_scope():
    for scope, states in STATIC_STATES.items():
        codes = [s.code for s in states]
        assert len(codes) == len(set(codes)), f"duplicate code in {scope}"


def test_names_and_codes_nonempty():
    for scope, states in STATIC_STATES.items():
        for s in states:
            assert s.code and s.name, f"{scope}: {s}"


def test_known_district_state_codes():
    by_name = {s.name: s.code for s in STATIC_STATES["district"]}
    # Numeric eCourts codes are NOT sequential with the alphabet -- these are
    # the real captured values and are what get passed to list_districts().
    assert by_name["Delhi"] == "26"
    assert by_name["Maharashtra"] == "1"


def test_known_high_court_present():
    hc_names = {s.name for s in STATIC_STATES["highcourt"]}
    assert "High Court of Delhi" in hc_names
    assert "Bombay High Court" in hc_names
