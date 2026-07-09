"""Unit tests for the district `cases_new.php` cause-list parser.

Fixtures are REAL eCourts mobile-API v4 responses captured live against Delhi
New Delhi district / Patiala House, court_no 75 ("District Judge-04") on
2026-07-03, after the 2026-07-02 v3->v4 cutover (the `token` field stripped):

- ``cause_list_v4_civil.json`` -- a populated civil list (``cases_list`` is an
  object keyed case1..case20, each entry carrying ``cino`` = the CNR).
- ``cause_list_v4_empty.json`` -- a court with no listing for that date+flag
  (``cases_list`` is an empty ``[]``, but ``designation_name`` is still present).

Before the v4 fix, ``parse_cause_list`` expected ``cases`` to be an HTML string
and returned ZERO entries for every v4 response -- which silently disabled the
entire district cause-list digest. These tests pin the v4 shape.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ecourts_client.parsers.cause_list import parse_cause_list

_FIX = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


_KW = dict(state_code="26", district_code="7", court_code="1", court_no="75",
           list_date=date(2026, 7, 3), flag="civ_t")


def test_v4_populated_parses_all_entries():
    cl = parse_cause_list(_load("cause_list_v4_civil.json"), **_KW)
    # designation_name (NOT the officer's personal judge_name) -> the seed key.
    assert cl.judge == "District Judge-04"
    assert len(cl.entries) == 20


def test_v4_first_entry_fields():
    cl = parse_cause_list(_load("cause_list_v4_civil.json"), **_KW)
    e = cl.entries[0]
    assert e.sr_no == 1
    assert e.cnr == "DLND010001442017"        # from cino, upper-cased
    assert e.case_number == "CS/17/2017"       # formatted, NOT the numeric case-id
    assert "RATTAN SINGH" in e.parties
    assert e.section == "Misc. cases"


def test_v4_every_entry_carries_cnr():
    """The CNR is the reliable match key for the indexer, so every row must have it."""
    cl = parse_cause_list(_load("cause_list_v4_civil.json"), **_KW)
    assert all(e.cnr for e in cl.entries)
    assert all(e.cnr == e.cnr.upper() for e in cl.entries)


def test_v4_empty_list_yields_no_entries_but_keeps_judge():
    cl = parse_cause_list(_load("cause_list_v4_empty.json"),
                          **{**_KW, "flag": "cri_t"})
    assert cl.entries == []
    assert cl.judge == "District Judge-04"      # empty list still names the court


def test_no_cause_list_false_sentinel():
    """eCourts returns cases=False (or null) when there is genuinely no court/list."""
    cl = parse_cause_list({"cases": False}, **{**_KW, "court_no": "999"})
    assert cl.judge is None
    assert cl.entries == []


def test_v4_row_with_cino_but_blank_sr_no_is_kept():
    """A real listing carrying a CNR must survive even if sr_no is missing/blank.

    Dropping it (the pre-review behavior) silently omits the user's hearing --
    the exact outage class this fix targets. sr_no is display-only -> defaults 0.
    """
    resp = {"cases": {"designation_name": "District Judge-04", "cases_list": {
        "case1": {"sr_no": "", "cino": "DLND010001442017",
                  "case_number": "CS/1/2020", "cause_title": "A vs B",
                  "purpose_name": "Arguments"},
        "case2": {"sr_no": None, "cino": "DLND010009992021",
                  "case_number": "CS/2/2021", "cause_title": "C vs D"},
    }}}
    cl = parse_cause_list(resp, **_KW)
    assert len(cl.entries) == 2
    assert cl.entries[0].cnr == "DLND010001442017"
    assert cl.entries[0].sr_no == 0        # position unknown, row kept


def test_v4_row_with_no_ids_is_dropped():
    """A row with neither cino nor case_number can't match a saved case -> drop."""
    resp = {"cases": {"designation_name": "District Judge-04", "cases_list": {
        "case1": {"sr_no": 1, "cause_title": "no identifiers"},
    }}}
    cl = parse_cause_list(resp, **_KW)
    assert cl.entries == []


def test_v4_whitespace_designation_falls_back_to_judge_name():
    """A whitespace-only designation must fall back to judge_name, not yield None
    (judge=None disables the courtroom's VC resolution downstream)."""
    resp = {"cases": {"designation_name": "   ", "judge_name": "Ms. Somebody",
                      "cases_list": []}}
    cl = parse_cause_list(resp, **_KW)
    assert cl.judge == "Ms. Somebody"


def test_v4_populated_but_unparseable_logs_drift(caplog):
    """A non-empty cases_list that parses to zero entries warns about schema drift."""
    import logging
    resp = {"cases": {"designation_name": "District Judge-04", "cases_list": {
        "case1": {"sr_no": 1},  # no cino / no case_number -> dropped
    }}}
    with caplog.at_level(logging.WARNING):
        cl = parse_cause_list(resp, **_KW)
    assert cl.entries == []
    assert any("schema drift" in r.getMessage() for r in caplog.records)


def test_v3_html_fallback_still_works():
    """Legacy v3 HTML payloads must still parse (defensive dual-format support)."""
    html = (
        "<div id='table_heading'><center><center>Sh. Test Judge</center>"
        "Civil Cases Listed on 03-07-2026</center></div>"
        "<table><tbody><tr>"
        "<td>1</td>"
        "<td>&nbsp;<a class='case_history_link' court_code='1' "
        "case_no='200400000672025' cino='dlnd010051812025'>CS/67/2025</a><br/>14-07-2025</td>"
        "<td>A<br/>versus<br/>B</td><td>ADV</td>"
        "</tr></tbody></table>"
    )
    cl = parse_cause_list({"cases": html}, **_KW)
    assert len(cl.entries) == 1
    assert cl.entries[0].case_number == "200400000672025"
    assert cl.entries[0].cnr == "DLND010051812025"
