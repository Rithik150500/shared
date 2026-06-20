"""Unit tests for parse_case_history — filing_date extraction.

The case-detail API response carries `date_of_filing`; the parser maps it to
Case.filing_date so the downstream "Filed" timeline event can render.
"""
from datetime import date

from ecourts_client.parsers.case_history import parse_case_history


def _response(**history_overrides):
    history = {
        "pet_name": "ABC Pvt Ltd",
        "res_name": "XYZ Corp",
        "court_name": "District Court",
        "district_name": "Central",
        "state_name": "Delhi",
        "purpose_name": "Misc. cases",
        "desgname": "District Judge-01",
        "date_next_list": "Date Not Given",
    }
    history.update(history_overrides)
    return {"history": history}


def test_parse_case_history_extracts_filing_date():
    case = parse_case_history(_response(date_of_filing="2025-07-09"), "DLHC010001232024")
    assert case.filing_date == date(2025, 7, 9)


def test_parse_case_history_filing_date_none_when_missing():
    case = parse_case_history(_response(), "DLHC010001232024")
    assert case.filing_date is None


def test_parse_case_history_filing_date_none_for_placeholder():
    case = parse_case_history(_response(date_of_filing="Date Not Given"), "DLHC010001232024")
    assert case.filing_date is None
