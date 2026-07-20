"""Court-key derivation for per-court circuit breakers.

A key becomes a PERMANENT entry in the process-wide breaker registry, so the
function must be TOTAL (never raise) and must never echo a caller-supplied
string back as a key -- every component is validated against a closed set.
"""
from __future__ import annotations

import pytest

from ecourts_client.resilience.court_key import (
    GLOBAL_KEY,
    UNKNOWN_KEY,
    court_key_for_cnr,
    is_court_scoped,
)


@pytest.mark.parametrize(
    "cnr,expected",
    [
        # District: state code in chars 0:2, validated against STATE_CODES.
        ("MHAU019999992015", "dc:MH"),
        # District with a NUMERIC establishment segment (Madhya Pradesh).
        ("MP20060042872025", "dc:MP"),
        # High Court form 1 -- [STATE][HC], 'HC' in chars 2:4.
        ("DLHC010012342023", "hc:DL"),
        # Form 1 with an HC-specific code in the state slot (Punjab & Haryana).
        ("PHHC010012342023", "hc:PH"),
        # High Court form 2 -- [HC][bench], literal 'HC' in the STATE slot.
        ("HCBM010012342023", "hc:BM"),
        ("HCMA010012342023", "hc:MA"),
    ],
    ids=["district", "district-numeric-estab", "hc-form1", "hc-form1-ph",
         "hc-form2-bombay", "hc-form2-madras"],
)
def test_derives_a_stable_state_or_bench_level_key(cnr, expected):
    assert court_key_for_cnr(cnr) == expected


@pytest.mark.parametrize(
    "bad",
    [None, "", "nope", "MH123", 12345, object(), "mhau019999992015",
     "MHAU01999999201", "MHAU0199999920155"],
    ids=["none", "empty", "short-word", "too-short", "int", "object",
         "lowercase", "15-chars", "17-chars"],
)
def test_is_total_and_never_raises(bad):
    """Malformed input must yield the sentinel, never an exception."""
    assert court_key_for_cnr(bad) == UNKNOWN_KEY


def test_unknown_state_code_does_not_mint_a_key():
    """A shape-valid CNR with a bogus state must NOT create dc:ZZ.

    Otherwise a user typing junk CNRs could mint unbounded registry entries
    and unbounded Prometheus label values.
    """
    assert court_key_for_cnr("ZZAU019999992015") == UNKNOWN_KEY


def test_hc_slots_must_be_letters():
    """Digits in the HC bench/state slot are not a real bench -> sentinel."""
    assert court_key_for_cnr("HC12010012342023") == UNKNOWN_KEY


def test_key_space_is_bounded_and_never_echoes_input():
    """Every derived key is a prefix plus exactly two validated chars."""
    for cnr in ("MHAU019999992015", "DLHC010012342023", "HCBM010012342023"):
        key = court_key_for_cnr(cnr)
        assert key.split(":")[0] in {"dc", "hc"}
        assert len(key.split(":")[1]) == 2
        assert cnr not in key


def test_is_court_scoped_distinguishes_court_keys_from_the_global_one():
    assert is_court_scoped("dc:MH") is True
    assert is_court_scoped("hc:BM") is True
    assert is_court_scoped(GLOBAL_KEY) is False
    assert is_court_scoped("forum_consumer") is False
