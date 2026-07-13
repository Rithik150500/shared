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


def test_parse_case_history_captures_routing_facts():
    from ecourts_client.parsers.case_history import parse_case_history
    resp = {"history": {
        "cino": "DLND010051812025", "case_no": "200400000672025",
        "court_no": "75", "desgname": "District Judge-04",
        "pet_name": "A", "res_name": "B", "date_next_list": "2026-07-15",
        "finalOrder": [{"order_id": "1", "order_date1": "2026-04-08",
                        "filename": "/orders/2026/x_1.pdf",
                        "state_cd": "26", "dist_cd": "3", "court_code": "1"}],
    }}
    case = parse_case_history(resp, cnr="DLND010051812025")
    assert case.court_no == "75"
    assert case.case_no == "200400000672025"
    assert (case.state_code, case.district_code, case.court_code) == ("26", "3", "1")


# --- District v4 historyOfCaseHearing (JSON list) parsing -------------------
# Real captured row shape (caseHistoryWebService.php, CNR JHDG010003692024):
# the hearing date is in ``todays_date`` (DD-MM-YYYY); ``nextdate`` is the
# *next* date in YYYYMMDD and must NOT be read as the hearing date, or every
# district hearing row silently drops (unparseable YYYYMMDD -> None -> skipped).

def _district_hearing_row(**overrides):
    row = {
        "purpose": "HEARING",
        "nextdate": "20260713",       # next date, YYYYMMDD -- NOT the hearing date
        "n_dt": "20260713",
        "judge_name": "District and Addl. Sessions Judge",
        "todays_date": "08-06-2026",  # the actual hearing date, DD-MM-YYYY
        "todays_date1": "08-06-2026",
        "businessStatus": "Pending",
        "court_no": "2",
        "cino": "JHDG010003692024",
    }
    row.update(overrides)
    return row


def test_parse_history_v4_district_hearing_dated_from_todays_date():
    resp = _response(historyOfCaseHearing=[_district_hearing_row()])
    case = parse_case_history(resp, "JHDG010003692024")
    assert len(case.history) == 1
    assert case.history[0].hearing_date == date(2026, 6, 8)


def test_parse_history_v4_district_hearing_judge_from_judge_name():
    resp = _response(historyOfCaseHearing=[
        _district_hearing_row(judge_name="District and Addl. Sessions Judge I")])
    case = parse_case_history(resp, "JHDG010003692024")
    assert len(case.history) == 1
    assert case.history[0].judge == "District and Addl. Sessions Judge I"
