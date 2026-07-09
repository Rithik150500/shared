"""Parser for the HC cause-list index (cases_new.php in HC scope).

Returns a list of HCCauseListIndex rows. Each row points to a downloadable PDF
whose contents must be parsed separately (deferred per docs/DEFERRED.md -- needs
pdfplumber tuning per-bench layout).

Companion parser `parse_hc_bench_sittings` handles the
causeListBenchWebService.php response, which packs benches into a `~`-separated
string list (same hash-list pattern as police_stations / case_types).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import HCBenchSitting, HCCauseListIndex
from ecourts_client.parsers.dropdowns_extra import _parse_hash_list


def parse_hc_bench_sittings(
    response: dict[str, Any], *, state_code: str, sitting_date: date
) -> list[HCBenchSitting]:
    """The bench-sittings endpoint returns `{benches: {benchesStr: 'code1~name1#...'}}`
    OR `{benches: null}` on holidays / non-sitting days.
    """
    payload = response.get("benches")
    if not isinstance(payload, dict):
        # null / missing -- no benches sitting
        return []
    packed = payload.get("benchesStr")
    if not isinstance(packed, str):
        return []
    # HC bench webservice separator is '^' not '#' (different from police_stations).
    # Each entry: 'code~display_text' (display_text often contains the code repeated at the end).
    out: list[HCBenchSitting] = []
    for entry in packed.split("^"):
        if not entry or "~" not in entry:
            continue
        code, name = entry.split("~", 1)
        out.append(HCBenchSitting(
            code=code.strip(), name=name.strip(),
            state_code=state_code, sitting_date=sitting_date,
        ))
    return out


def parse_hc_cause_list_index(response: dict[str, Any]) -> list[HCCauseListIndex]:
    """Parse the HC cause-list index (cases_new.php, HC scope) into structured rows.

    eCourts mobile API v4.0 (cutover 2026-07-02) replaced the legacy v3 HTML
    table with a structured-JSON payload (verified live against Delhi HC,
    2026-06-30 -- see docs/RE_NOTES_v4.md)::

        {"cases": {"cases_list": {
            "case1": {"sr_no": 1,
                      "bench": "HON'BLE MS. JUSTICE ...",
                      "cause_list_type": "COMPLETE CAUSE LIST",
                      "cause_list_url": ".../causelist_pdf.php?params=...&authtoken=..."},
            "case2": {...}}}}

    Each entry's ``cause_list_url`` is a directly-fetchable signed
    ``causelist_pdf.php`` link (a plain GET returns application/pdf -- no
    display_pdf_new.php two-step, unlike order PDFs), so it maps straight onto
    ``HCCauseListIndex.pdf_url`` for ``fetch_cause_list_pdf_rows``.

    The pre-v4 HTML-table shape (``cases`` as an HTML string, scraped for the
    <a href> in the "View Causelist" column) is still accepted defensively --
    the v4 rollout is per-court, so some benches may keep serving HTML.
    """
    if not isinstance(response, dict):
        raise SchemaChanged(field="response", reason=f"expected dict, got {type(response).__name__}")
    cases = response.get("cases")

    # v4.0 structured-JSON shape.
    if isinstance(cases, dict):
        return _parse_index_json(cases)

    # Legacy v3 HTML-table shape.
    if isinstance(cases, str) and cases.strip():
        return _parse_index_html(cases)

    # null / missing / empty -> no cause list published for this bench+date.
    return []


def _parse_index_json(cases: dict[str, Any]) -> list[HCCauseListIndex]:
    """v4.0 ``{"cases_list": {"caseN": {...}}}`` -> HCCauseListIndex rows.

    Dict insertion order is the API's declared row order (case1, case2, ...).
    """
    cases_list = cases.get("cases_list")
    if not isinstance(cases_list, dict):
        return []
    rows: list[HCCauseListIndex] = []
    for entry in cases_list.values():
        if not isinstance(entry, dict):
            continue
        pdf_url = str(entry.get("cause_list_url") or "").strip()
        if not pdf_url:
            continue
        try:
            sr_no = int(entry.get("sr_no"))
        except (TypeError, ValueError):
            continue
        rows.append(HCCauseListIndex(
            sr_no=sr_no,
            bench=str(entry.get("bench") or "").strip(),
            list_type=str(entry.get("cause_list_type") or "").strip(),
            pdf_url=pdf_url,
        ))
    return rows


def _parse_index_html(html: str) -> list[HCCauseListIndex]:
    """Legacy v3 HTML-table shape: Sr No | Bench | Cause List Type | <a href> PDF."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[HCCauseListIndex] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        sr_text = cells[0].get_text(strip=True)
        try:
            sr_no = int(sr_text)
        except ValueError:
            continue
        bench = cells[1].get_text(strip=True)
        list_type = cells[2].get_text(strip=True)
        link = cells[3].find("a")
        pdf_url = (link.get("href") or "").strip() if link is not None else ""
        if not pdf_url:
            continue
        rows.append(HCCauseListIndex(sr_no=sr_no, bench=bench, list_type=list_type, pdf_url=pdf_url))
    return rows
