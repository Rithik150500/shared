""""No benches sitting" and "we could not read the answer" are different facts.

``parse_hc_bench_sittings`` used to return ``[]`` for BOTH, and every caller
reads ``[]`` as a holiday. On prod that hid a fleet-wide cause-list blackout for
four consecutive working days (2026-07-28..07-31): ``cause_list_rows`` held
nothing for those listing dates while the cron fired daily and the droplet could
see all 75 Delhi HC benches when asked directly.

Only the two shapes eCourts actually uses to say "nothing today" -- a null
``benches``, or an empty ``benchesStr`` -- return ``[]``. Anything else raises
``SchemaChanged``, which is still an ``ECourtsError`` (so ``index_for_date``
keeps catching it and one bad HC cannot abort the nightly run) but is loud and
classifies NEUTRAL for the circuit breaker rather than as a fake outage.
"""
from __future__ import annotations

from datetime import date

import pytest

from ecourts_client.errors import ECourtsError, SchemaChanged
from ecourts_client.parsers.cause_list_hc import parse_hc_bench_sittings
from ecourts_client.resilience.failure_policy import Outcome, classify_failure


_TARGET = date(2026, 7, 31)


def _parse(payload):
    return parse_hc_bench_sittings(payload, state_code="26", sitting_date=_TARGET)


class TestGenuinelyEmpty:
    def test_null_benches_is_a_holiday(self):
        """The documented non-sitting-day answer."""
        assert _parse({"benches": None}) == []

    def test_empty_benchesstr_is_a_holiday(self):
        assert _parse({"benches": {"benchesStr": ""}}) == []

    def test_whitespace_benchesstr_is_a_holiday(self):
        assert _parse({"benches": {"benchesStr": "   "}}) == []

    def test_null_benchesstr_is_a_holiday(self):
        """Live shape for future/vacation dates: the wrapper dict is present
        but benchesStr is null. Captured in test_parse_cause_list_hc.py."""
        assert _parse({"benches": {"benchesStr": None}}) == []


class TestDriftIsLoud:
    def test_missing_benches_key(self):
        with pytest.raises(SchemaChanged):
            _parse({})

    def test_benches_is_a_list(self):
        with pytest.raises(SchemaChanged):
            _parse({"benches": ["11865"]})

    def test_missing_benchesstr_key(self):
        with pytest.raises(SchemaChanged):
            _parse({"benches": {"somethingElse": "x"}})

    def test_non_string_benchesstr(self):
        with pytest.raises(SchemaChanged):
            _parse({"benches": {"benchesStr": 42}})

    def test_separator_drift_is_not_a_holiday(self):
        """The shape that would dark every court at once with no signal: a
        payload full of data that our framing no longer understands."""
        with pytest.raises(SchemaChanged):
            _parse({"benches": {"benchesStr": "11865|JUSTICE X#11866|JUSTICE Y"}})


class TestStillParsesRealPayloads:
    def test_two_benches(self):
        got = _parse({"benches": {
            "benchesStr": "11865~JUSTICE JYOTI SINGH 11865^12018~JUSTICE X 12018",
        }})
        assert [b.code for b in got] == ["11865", "12018"]
        assert got[0].name == "JUSTICE JYOTI SINGH 11865"
        assert got[0].state_code == "26"
        assert got[0].sitting_date == _TARGET

    def test_single_bench(self):
        got = _parse({"benches": {"benchesStr": "5226~SPECIAL BENCHES 5226"}})
        assert [b.code for b in got] == ["5226"]

    def test_trailing_separator_tolerated(self):
        got = _parse({"benches": {"benchesStr": "5226~SPECIAL BENCHES 5226^"}})
        assert [b.code for b in got] == ["5226"]


class TestBlastRadius:
    def test_schema_changed_is_still_an_ecourts_error(self):
        """index_for_date catches ECourtsError per HC, so one drifted court
        must not abort the other 24."""
        assert issubclass(SchemaChanged, ECourtsError)

    def test_does_not_trip_a_breaker(self):
        """Our parser being out of date is not an availability signal."""
        assert classify_failure(SchemaChanged("benches", "x")) is Outcome.NEUTRAL
