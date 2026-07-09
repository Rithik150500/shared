"""Unit tests for Case.to_json / Case.from_json round-trip.

FIX 2: routing fields (case_no, court_no, state_code, district_code, court_code)
were serialised by to_json (via asdict) but silently dropped by _case_from_dict
(it passed no kwargs for them). A CachedECourtsClient round-trip would produce
None for all five fields, breaking cause-list routing.
"""
from datetime import date

from ecourts_client.models import Case


def _make_case(**overrides) -> Case:
    base = dict(
        cnr="DLND010051812025",
        title="A vs B",
        court="District Court, Central",
        stage="Final Hearing",
        next_hearing_date=date(2026, 8, 1),
        judge="District Judge-04",
    )
    base.update(overrides)
    return Case(**base)


def test_routing_fields_survive_json_roundtrip():
    """All five routing fields must be preserved across to_json / from_json."""
    original = _make_case(
        case_no="200400000672025",
        court_no="75",
        state_code="26",
        district_code="3",
        court_code="1",
    )
    restored = Case.from_json(original.to_json())

    assert restored.case_no == "200400000672025", (
        f"case_no lost: got {restored.case_no!r}"
    )
    assert restored.court_no == "75", (
        f"court_no lost: got {restored.court_no!r}"
    )
    assert restored.state_code == "26", (
        f"state_code lost: got {restored.state_code!r}"
    )
    assert restored.district_code == "3", (
        f"district_code lost: got {restored.district_code!r}"
    )
    assert restored.court_code == "1", (
        f"court_code lost: got {restored.court_code!r}"
    )


def test_routing_fields_default_none_when_absent_in_json():
    """Older cached JSONs without routing keys must deserialise to None (not KeyError)."""
    original = _make_case()  # no routing fields set → all None
    restored = Case.from_json(original.to_json())
    assert restored.case_no is None
    assert restored.court_no is None
    assert restored.state_code is None
    assert restored.district_code is None
    assert restored.court_code is None


def test_other_fields_unaffected_by_routing_roundtrip():
    """Core fields (title, stage, next_hearing_date, filing_date) must survive
    alongside routing fields — regression guard."""
    original = _make_case(
        court_no="10",
        filing_date=date(2025, 1, 15),
    )
    restored = Case.from_json(original.to_json())
    assert restored.cnr == original.cnr
    assert restored.title == original.title
    assert restored.next_hearing_date == date(2026, 8, 1)
    assert restored.filing_date == date(2025, 1, 15)
    assert restored.court_no == "10"
