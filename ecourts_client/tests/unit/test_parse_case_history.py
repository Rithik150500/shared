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


# --- Disposed cases must NOT carry a next_hearing_date ----------------------
# eCourts v4 echoes the disposal / last-listing date into ``date_next_list`` on
# a disposed case (there is no future listing). Reading it into
# Case.next_hearing_date makes the web/WhatsApp UI render a phantom
# "Next Hearing (OVERDUE)" at the disposal date. The tribunal parser already
# nulls the next date on disposal; district/HC must too.

def _disposed_hearing_row(**overrides):
    row = {
        "purpose": "Disposed",          # the disposed-state token (last listing)
        "todays_date": "16-01-2025",    # hearing date, DD-MM-YYYY
        "judge_name": "District Judge",
        "cino": "DLNT010103162024",
    }
    row.update(overrides)
    return row


def test_disposed_district_case_nulls_next_hearing_date():
    resp = _response(
        date_next_list="2025-01-16",  # stale echo of the disposal date
        historyOfCaseHearing=[
            _district_hearing_row(todays_date="13-01-2025", purpose="Misc. cases/purpose"),
            _disposed_hearing_row(),
        ],
    )
    case = parse_case_history(resp, "DLNT010103162024")
    assert case.next_hearing_date is None


def test_pending_case_with_future_date_and_disposal_purpose_keeps_next_hearing():
    # False-positive guard: "Final Disposal Misc." is a PENDING listing purpose
    # (word "disposal", not the "disposed" state token) and the next date is in
    # the future — this is an active case and must keep its next hearing.
    resp = _response(
        date_next_list="2026-07-15",
        historyOfCaseHearing=[_district_hearing_row(purpose="Final Disposal Misc.")],
    )
    case = parse_case_history(resp, "JHDG010003692024")
    assert case.next_hearing_date == date(2026, 7, 15)


def test_disposed_district_terminal_judgement_nulls_next_hearing():
    # "Judgement" is a terminal listing with no "disposed" substring; the
    # exact-token terminal set must still recognise it as disposal.
    resp = _response(
        date_next_list="2025-01-16",
        historyOfCaseHearing=[
            _district_hearing_row(todays_date="13-01-2025", purpose="Arguments"),
            _disposed_hearing_row(todays_date="16-01-2025", purpose="Judgement"),
        ],
    )
    case = parse_case_history(resp, "DLNT010103162024")
    assert case.next_hearing_date is None


def test_pending_district_interim_ia_disposed_keeps_next_hearing():
    # Interim "Disposed of IA No.5" as the latest row while the main case is
    # pending — exact-token match (not substring) must keep the next hearing.
    resp = _response(
        date_next_list="2026-09-01",
        historyOfCaseHearing=[_district_hearing_row(purpose="Disposed of IA No.5")],
    )
    case = parse_case_history(resp, "JHDG010003692024")
    assert case.next_hearing_date == date(2026, 9, 1)
