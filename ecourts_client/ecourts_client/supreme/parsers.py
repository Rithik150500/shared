"""Parse the SC mobile case-detail HTML (pageid=030001) into a generic ``Case``.

The page is a server-rendered ``<table>`` of label→value rows (Diary No.,
Case No., Present/Last Listed On, Status/Stage, Petitioner(s)/Respondent(s),
advocates). Schema-tolerant: unknown/renamed labels are ignored, missing values
degrade to None rather than crashing.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ecourts_client.errors import CNRNotFound
from ecourts_client.models import Case, Party

_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
_LEAD_NUM = re.compile(r"^\s*\d+\s+")  # "1 ABDUL RAIHAN MIAN" -> "ABDUL RAIHAN MIAN"


def _first_date(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
    except ValueError:
        return None


def _clean_party(text: str | None) -> str:
    if not text:
        return ""
    # take the first listed name; strip a leading serial number
    first = text.split("\n")[0].strip()
    return _LEAD_NUM.sub("", first).strip()


def _bench(listed: str | None) -> str | None:
    """Extract the bench (judges) from the '... [ HON'BLE ... ]' suffix."""
    if not listed:
        return None
    m = re.search(r"\[(.+?)\]?$", listed)
    if not m:
        return None
    b = m.group(1).strip().rstrip("]").strip()
    return b or None


def _clean_case_no(text: str | None) -> str:
    """'SLP(Crl) No. 003159 -  / 2026  Registered on ...' -> 'SLP(Crl) No. 003159/2026'."""
    if not text:
        return ""
    head = re.split(r"\bRegistered\b|\bVerified\b", text)[0]
    head = re.sub(r"\s*-\s*/\s*", "/", head)  # "003159 -  / 2026" -> "003159/2026"
    return re.sub(r"\s+", " ", head).strip()


def _rows(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            k = tds[0].get_text(" ", strip=True).rstrip(":").strip()
            v = tds[1].get_text(" ", strip=True)
            if k and k not in out:
                out[k] = v
    return out


def parse_case_html(html: str, *, diary_no: str, diary_yr: str) -> Case:
    """Parse the pageid=030001 case-detail HTML into a ``Case``.

    ``cnr`` carries the diary ``<no>/<yr>`` (the stable per-forum key + the value
    ``fetch_case`` round-trips on). Raises ``CNRNotFound`` when the page has no
    case rows (unknown diary / empty result)."""
    d = _rows(html)
    # A valid case page has these labels; their absence = no such case.
    if not any(k in d for k in ("Diary No.", "Case No.", "Petitioner(s)", "Status/Stage")):
        raise CNRNotFound(cnr=f"{diary_no}/{diary_yr}")

    pet = _clean_party(d.get("Petitioner(s)"))
    res = _clean_party(d.get("Respondent(s)"))
    parties: list[Party] = []
    if pet:
        parties.append(Party(name=pet, role="petitioner", advocate=d.get("Pet. Advocate(s)") or None))
    if res:
        parties.append(Party(name=res, role="respondent", advocate=d.get("Resp. Advocate(s)") or None))

    case_no = _clean_case_no(d.get("Case No."))
    title = f"{pet} vs {res}" if pet and res else (pet or res or case_no or f"Diary {diary_no}/{diary_yr}")
    listed = d.get("Present/Last Listed On")

    return Case(
        cnr=f"{diary_no}/{diary_yr}",
        title=title,
        court="Supreme Court of India",
        stage=(d.get("Status/Stage") or "").strip() or None,
        next_hearing_date=_first_date(listed),
        judge=_bench(listed),
        parties=parties,
        history=[],
        orders=[],
        filing_date=_first_date(d.get("Diary No.")),  # "... Filed on DD-MM-YYYY ..."
    )
