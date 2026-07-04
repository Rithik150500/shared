"""Unit tests for the HC cause-list index + bench-sittings parsers.

Fixtures capture the real eCourts mobile API v4.0 shapes observed live against
Delhi HC (state_code 26) on 2026-06-30, after the 2026-07-02 v3->v4 cutover:

- ``causeListBenchWebService.php`` still returns
  ``{benches: {benchesStr: "code~name^code~name..."}}`` (unchanged from v3).
- ``cases_new.php`` now returns structured JSON
  ``{cases: {cases_list: {caseN: {sr_no, bench, cause_list_type, cause_list_url}}}}``
  where it previously returned an HTML table. ``cause_list_url`` is a directly
  fetchable signed ``causelist_pdf.php`` link.

See docs/RE_NOTES_v4.md.
"""
from datetime import date

from ecourts_client.parsers.cause_list_hc import (
    parse_hc_bench_sittings,
    parse_hc_cause_list_index,
)


# --- v4 index (cases_new.php) -------------------------------------------------

_V4_INDEX = {
    "cases": {
        "cases_list": {
            "case1": {
                "sr_no": 1,
                "bench": "HON'BLE MS. JUSTICE PRATHIBA M. SINGH, HON'BLE MR. JUSTICE AMIT SHARMA",
                "cause_list_type": "COMPLETE CAUSE LIST",
                "cause_list_url": (
                    "https://app.ecourts.gov.in/services_HC_4.0/causelist_pdf.php"
                    "?params=abc123&authtoken=def456"
                ),
            },
            "case2": {
                "sr_no": 2,
                "bench": "PRE LOK ADALAT",
                "cause_list_type": "SUPPLEMENTARY CAUSE LIST",
                "cause_list_url": (
                    "https://app.ecourts.gov.in/services_HC_4.0/causelist_pdf.php"
                    "?params=ghi789&authtoken=jkl012"
                ),
            },
        }
    }
}


def test_parse_index_v4_json_extracts_rows_in_order():
    rows = parse_hc_cause_list_index(_V4_INDEX)
    assert len(rows) == 2
    assert rows[0].sr_no == 1
    assert rows[0].bench.startswith("HON'BLE MS. JUSTICE PRATHIBA")
    assert rows[0].list_type == "COMPLETE CAUSE LIST"
    assert rows[0].pdf_url.endswith("authtoken=def456")
    assert rows[1].sr_no == 2
    assert rows[1].list_type == "SUPPLEMENTARY CAUSE LIST"
    assert rows[1].pdf_url.startswith(
        "https://app.ecourts.gov.in/services_HC_4.0/causelist_pdf.php"
    )


def test_parse_index_v4_skips_rows_without_url():
    resp = {
        "cases": {
            "cases_list": {
                "case1": {"sr_no": 1, "bench": "B", "cause_list_type": "T", "cause_list_url": ""},
                "case2": {"sr_no": 2, "bench": "B2", "cause_list_type": "T2",
                          "cause_list_url": "https://x/causelist_pdf.php?p=1"},
            }
        }
    }
    rows = parse_hc_cause_list_index(resp)
    assert [r.sr_no for r in rows] == [2]


def test_parse_index_v4_skips_non_integer_sr_no():
    resp = {"cases": {"cases_list": {"case1": {
        "sr_no": "N/A", "bench": "B", "cause_list_type": "T",
        "cause_list_url": "https://x/causelist_pdf.php?p=1"}}}}
    assert parse_hc_cause_list_index(resp) == []


def test_parse_index_empty_when_no_cause_list():
    # null cases (no list published for this bench/date), missing key, empty list.
    assert parse_hc_cause_list_index({"cases": None}) == []
    assert parse_hc_cause_list_index({}) == []
    assert parse_hc_cause_list_index({"cases": {"cases_list": {}}}) == []
    assert parse_hc_cause_list_index({"cases": {}}) == []


def test_parse_index_v3_html_still_supported():
    # v4 rollout is per-court; a bench still on v3 serves an HTML table.
    html = (
        "<table>"
        "<tr><td>1</td><td>Bench A</td><td>FRESH MOTION</td>"
        "<td><a href='https://x/pdf?p=1'>View</a></td></tr>"
        "<tr><td>2</td><td>Bench B</td><td>REGULAR</td>"
        "<td><a href='https://x/pdf?p=2'>View</a></td></tr>"
        "</table>"
    )
    rows = parse_hc_cause_list_index({"cases": html})
    assert [r.sr_no for r in rows] == [1, 2]
    assert rows[0].bench == "Bench A"
    assert rows[0].list_type == "FRESH MOTION"
    assert rows[0].pdf_url == "https://x/pdf?p=1"


# --- bench sittings (causeListBenchWebService.php) — unchanged under v4 -------

def test_parse_bench_sittings_v4_populated():
    resp = {"benches": {"benchesStr": (
        "6341~PRE LOK ADALAT 6341"
        "^11886~HON'BLE MS. JUSTICE PRATHIBA M. SINGH, HON'BLE MR. JUSTICE AMIT SHARMA 11886"
    )}}
    benches = parse_hc_bench_sittings(resp, state_code="26", sitting_date=date(2026, 6, 30))
    assert [b.code for b in benches] == ["6341", "11886"]
    assert benches[0].name.startswith("PRE LOK ADALAT")
    assert benches[0].state_code == "26"
    assert benches[0].sitting_date == date(2026, 6, 30)


def test_parse_bench_sittings_null_on_non_sitting_day():
    # Future/vacation dates: benchesStr is null.
    assert parse_hc_bench_sittings(
        {"benches": {"benchesStr": None}}, state_code="26", sitting_date=date(2026, 7, 6)
    ) == []
    assert parse_hc_bench_sittings(
        {"benches": None}, state_code="26", sitting_date=date(2026, 7, 6)
    ) == []
