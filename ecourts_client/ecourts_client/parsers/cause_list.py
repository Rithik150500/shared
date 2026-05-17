"""Parser for `cases_new.php` -- the per-courtroom cause list.

The response is `{"cases": "<inline HTML>"}`. The HTML is structured as:

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

import re
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import CauseList, CauseListEntry


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
    html = response.get("cases")
    if not isinstance(html, str):
        # Server returns False (or null) when there's no cause list for that date+court.
        return CauseList(
            state_code=state_code, district_code=district_code,
            court_code=court_code, court_no=court_no,
            list_date=list_date, flag=flag, judge=None, entries=[],
        )

    soup = BeautifulSoup(html, "lxml")
    judge = _extract_judge(soup)
    entries = _extract_entries(soup)

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
