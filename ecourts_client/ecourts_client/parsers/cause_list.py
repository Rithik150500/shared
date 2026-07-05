"""Parser for `cases_new.php` -- the per-courtroom cause list.

**eCourts v4 (2026-07-02 cutover):** the response is now
`{"cases": {"judge_name": ..., "designation_name": "District Judge-04",
            "casesHeading": "...", "cases_list": {"case1": {...}, ...}}}`
-- a structured JSON dict. `cases_list` is an OBJECT keyed case1..caseN when
populated and an empty LIST `[]` when the court has no listing for that
date+flag. Each entry carries `cino` (the CNR), `case_number`, `sr_no`,
`cause_title`, `main_advocate_names`, and `purpose_name`. Crucially the payload
includes both the officer's personal `judge_name` AND the judicial
`designation_name`; because the district VC directory is keyed by DESIGNATION,
we surface `designation_name` as `CauseList.judge` so the indexer can resolve
the VC link. See `_parse_v4_cases`.

**Legacy v3 (pre-cutover, retained as a fallback):** the response was
`{"cases": "<inline HTML>"}`. The HTML is structured as:

    <div id='table_heading'>
        <center><center>Sh. <Judge Name><br/>Title</center>
        <Civil|Criminal> Cases Listed on&nbsp;DD-MM-YYYY</center>
    </div>
    <table>
        <thead><th>Sr No</th><th>Case Number</th><th>Party Name</th><th>Advocate Name</th></thead>
        <tbody>
            <tr><td colspan='4'>Section Name</td></tr>  <!-- section header -->
        </tbody>
        <tbody>
            <tr>
                <td>1</td>
                <td>&nbsp;<a class='case_history_link' court_code='1' case_no='200400000672025'>CS/67/2025</a><br/><br/>14-07-2025</td>
                <td>JASMOHAN SINGH<br/>versus<br/>NDMC</td>
                <td>VASU DEV<br/></td>
            </tr>
        </tbody>
        ...
    </table>

The CNR is NOT in the cause list -- only the uniform case_no. Callers needing the
CNR must do a follow-up listOfCasesWebService.php on (court_code, case_no).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import CauseList, CauseListEntry

log = logging.getLogger(__name__)


_DMY_RE = re.compile(r"\b(\d{2}-\d{2}-\d{4})\b")
_CASE_NO_RE = re.compile(r"case_no\s*=\s*['\"](\d+)['\"]")


def parse_cause_list(
    response: dict[str, Any],
    *,
    state_code: str,
    district_code: str,
    court_code: str,
    court_no: str,
    list_date: date,
    flag: str,
) -> CauseList:
    if not isinstance(response, dict):
        raise SchemaChanged(field="response", reason=f"expected dict, got {type(response).__name__}")
    cases = response.get("cases")

    if isinstance(cases, dict):
        # v4 (2026-07 cutover): structured JSON payload.
        judge, entries = _parse_v4_cases(cases)
    elif isinstance(cases, str):
        # v3 legacy: inline HTML table.
        soup = BeautifulSoup(cases, "lxml")
        judge = _extract_judge(soup)
        entries = _extract_entries(soup)
    else:
        # Server returns False (or null) when there's genuinely no court/list.
        judge, entries = None, []

    return CauseList(
        state_code=state_code,
        district_code=district_code,
        court_code=court_code,
        court_no=court_no,
        list_date=list_date,
        flag=flag,
        judge=judge,
        entries=entries,
    )


def _parse_v4_cases(cases: dict[str, Any]) -> tuple[str | None, list[CauseListEntry]]:
    """Parse the v4 `cases_new.php` district payload -> (judge_designation, entries).

    ``cases_list`` is an object keyed case1..caseN when populated, or an empty
    ``[]`` when the court has no listing. We surface ``designation_name`` (not
    the officer's personal ``judge_name``) as the judge, because the district VC
    directory is keyed by judicial designation. Each entry's ``cino`` is the CNR
    -- the reliable key for matching saved cases (``case_number`` is a formatted
    string like "CS/17/2017" that does NOT equal the uniform numeric case-id).
    """
    # Normalize BEFORE the `or` so a whitespace-only designation_name (truthy but
    # blank) falls back to judge_name instead of short-circuiting to None. The VC
    # directory is keyed by designation, so losing it disables the courtroom's VC
    # link; judge_name is only a human-readable fallback label.
    judge = (
        str(cases.get("designation_name") or "").strip()
        or str(cases.get("judge_name") or "").strip()
        or None
    )

    raw_list = cases.get("cases_list")
    if isinstance(raw_list, dict):
        items: list[Any] = list(raw_list.values())
    elif isinstance(raw_list, list):
        items = raw_list
    else:
        items = []

    entries: list[CauseListEntry] = []
    for c in items:
        if not isinstance(c, dict):
            continue
        cino = c.get("cino")
        cnr = (str(cino).strip().upper() or None) if cino else None
        case_number = str(c.get("case_number") or c.get("case_no") or "").strip()
        # A row is only useful if we can match it to a saved case (by CNR or, for
        # legacy v3 responses, by case_number). Skip rows with neither -- but do
        # NOT gate on sr_no: a valid-CNR row with a missing/blank sr_no is a real
        # listing and must still surface (dropping it re-creates the outage this
        # fix targets). sr_no is display-only; default it to 0 when unparseable.
        if not cnr and not case_number:
            continue
        try:
            sr_no = int(c.get("sr_no"))
        except (TypeError, ValueError):
            sr_no = 0  # position unknown; keep the row so the hearing still shows
        entries.append(CauseListEntry(
            sr_no=sr_no,
            case_number=case_number,
            cnr=cnr,
            parties=str(c.get("cause_title") or "").strip(),
            advocates=(str(c.get("main_advocate_names") or "").strip() or None),
            section=(str(c.get("purpose_name") or "").strip() or "Default"),
            listed_on=None,
        ))

    # Observability: a populated payload that parses to zero entries signals v4
    # schema drift (e.g. NIC renamed cases_list / cino) -- the silent-zero-rows
    # class this whole fix targets. Surface it rather than degrading silently.
    if items and not entries:
        log.warning(
            "cause_list v4: %d raw row(s) but 0 parsed -- possible schema drift",
            len(items),
        )
    return judge, entries


def _extract_judge(soup: BeautifulSoup) -> str | None:
    heading = soup.find(id="table_heading")
    if heading is None:
        return None
    # The judge's name is the first <center> inside the heading.
    inner = heading.find("center")
    if inner is None:
        return None
    inner_inner = inner.find("center")
    if inner_inner is None:
        text = inner.get_text("\n", strip=True)
    else:
        text = inner_inner.get_text("\n", strip=True)
    if not text:
        return None
    # Take just the first line (the name); subsequent lines are the role
    return text.splitlines()[0].strip() or None


def _extract_entries(soup: BeautifulSoup) -> list[CauseListEntry]:
    entries: list[CauseListEntry] = []
    current_section = "Default"

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        # Section header rows have a single colspan'd cell (no <a>, no <br/> internally usually)
        if len(cells) == 1 and cells[0].get("colspan"):
            section_text = cells[0].get_text(strip=True)
            if section_text:
                current_section = section_text
            continue
        if len(cells) < 3:
            continue

        sr_text = cells[0].get_text(strip=True)
        try:
            sr_no = int(sr_text)
        except ValueError:
            continue  # skip header / spacer rows

        case_cell = cells[1]
        case_link = case_cell.find("a")
        case_html_str = str(case_cell)
        case_no_match = _CASE_NO_RE.search(case_html_str)
        case_number = case_no_match.group(1) if case_no_match else (case_link.get_text(strip=True) if case_link else "")
        cnr_attr = case_link.get("cino") if case_link else None
        # listed_on date often appears as "DD-MM-YYYY" trailing the case_no anchor
        cell_text = case_cell.get_text(" ", strip=True)
        listed_match = _DMY_RE.search(cell_text)
        listed_on = _parse_dmy(listed_match.group(1)) if listed_match else None

        parties = cells[2].get_text("\n", strip=True)
        advocates = cells[3].get_text("\n", strip=True) if len(cells) > 3 else None

        entries.append(CauseListEntry(
            sr_no=sr_no,
            case_number=case_number,
            cnr=cnr_attr.upper() if cnr_attr else None,
            parties=parties,
            advocates=advocates or None,
            section=current_section,
            listed_on=listed_on,
        ))

    return entries


def _parse_dmy(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%d-%m-%Y").date()
    except ValueError:
        return None
