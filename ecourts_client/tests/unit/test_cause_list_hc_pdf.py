"""Tests for the HC cause-list PDF parser: HCCauseListPDFRow.court_no field
and the _court_no_from_line() helper.

Task A6 — capture the 'COURT NO. NN' header printed above each court block in
Delhi HC cause-list PDFs so that downstream VC-link keying (Task C2) can map a
row to its physical court room.
"""
from __future__ import annotations


def test_hccauselistpdfrow_has_court_no_default():
    from ecourts_client.models import HCCauseListPDFRow
    r = HCCauseListPDFRow(sr_no=1, section="X", case_number="WP/1/2026", raw_text="...")
    assert r.court_no is None


def test_court_no_from_line_extracts_header():
    from ecourts_client.parsers.cause_list_hc_pdf import _court_no_from_line
    assert _court_no_from_line("COURT NO. 26  HON'BLE MS. JUSTICE X") == "26"
    assert _court_no_from_line("COURT NO.137A  HON'BLE MR. JUSTICE Y") == "137A"
    assert _court_no_from_line("Court No 8") == "8"
    assert _court_no_from_line("1  W.P.(C)/100/2026  A vs B") is None
    assert _court_no_from_line("S.No.  Case Number  Party") is None
