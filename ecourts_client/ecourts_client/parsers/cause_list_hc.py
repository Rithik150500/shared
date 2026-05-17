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
    """Parse the HC cause-list index HTML into structured rows.

    The HTML table has 4 columns: Sr No | Bench | Cause List Type | View Causelist
    (with an <a href> in the last column pointing to the PDF).
    """
    if not isinstance(response, dict):
        raise SchemaChanged(field="response", reason=f"expected dict, got {type(response).__name__}")
    html = response.get("cases")
    if not isinstance(html, str) or not html.strip():
        return []

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
