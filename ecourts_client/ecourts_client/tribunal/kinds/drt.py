"""DRT / DRAT (Debt Recovery Tribunal / Appellate Tribunal) tribunal-kind adapter.

Verified transport (``docs/spike-tribunal-transport.md``): the public
``cis.drt.gov.in/drtlive`` case-information system, no login, no captcha, 2-hop —
  1. ``GET partyDetail.php?caseNo&caseType&year&sc&id=casetypewise`` → search row
     with a ``MORE DETAIL`` link ``popsurety_detailreport('<b64>')``.
  2. ``GET Misdetailreport.php?no=<b64>`` → the full CASE STATUS detail page.

ONE client serves both DRT and DRAT (identical transport; the location ``sc`` and
the case-type code set differ, and both ride in the identifier). Registered under
both ``(Forum.TRIBUNAL, DRT)`` and ``(Forum.TRIBUNAL, DRAT)``.

Identifier: ``"<sc>:<caseType>:<caseNo>:<year>"`` — ``sc`` = the drtlive schema
code (``delhi`` for a DRT, ``delhidrat`` for a DRAT); ``caseType`` = the numeric
code from that tier's set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

import requests
from bs4 import BeautifulSoup

from ecourts_client.errors import CNRNotFound, CourtSiteDown, ECourtsError, RateLimited
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind, TribunalKind
from ecourts_client.models import Case, HearingHistoryRow, Party
from ecourts_client.tribunal._html import (
    date_anchored_rows,
    extract_after_dash,
    label_value_map,
    parse_dmy,
)

BASE_URL = "https://cis.drt.gov.in/drtlive"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TIMEOUT = 45
_B64_RE = re.compile(r"popsurety_detailreport\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _split_identifier(identifier: str) -> tuple[str, str, str, str]:
    parts = (identifier or "").split(":")
    if len(parts) != 4 or not all(p.strip() for p in parts):
        raise ECourtsError(f"DRT identifier must be '<sc>:<caseType>:<caseNo>:<year>', got {identifier!r}")
    return tuple(p.strip() for p in parts)  # type: ignore[return-value]


def parse_detail_html(html: str, *, sc: str) -> Case:
    """Parse the DRT ``Misdetailreport.php`` CASE STATUS page into a ``Case``.
    Raises ``CNRNotFound`` when the page carries no case row."""
    soup = BeautifulSoup(html, "html.parser")
    d = label_value_map(soup)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    ref = d.get("Case Type/Case No/Year")
    if not ref:
        raise CNRNotFound(cnr=f"drt:{sc}")

    pet = extract_after_dash(text, "Petitioner Name")
    res = extract_after_dash(text, "Respondent Name")
    parties: list[Party] = []
    if pet:
        parties.append(Party(name=pet, role="petitioner"))
    if res:
        parties.append(Party(name=res, role="respondent"))

    is_drat = sc.lower().endswith("drat")
    court = ("Debt Recovery Appellate Tribunal" if is_drat else "Debt Recovery Tribunal") + f" ({sc})"

    # The DRT proceeding table's <tr> tags are unclosed (malformed HTML), so a
    # per-row sweep collapses; anchor on each DD/MM/YYYY cell instead. Layout is
    # [Court Name, Causelist Date, Purpose] → (court, date, purpose).
    history: list[HearingHistoryRow] = []
    for before, dstr, purpose in date_anchored_rows(soup, "Causelist Date"):
        hd = parse_dmy(dstr)
        if hd:
            history.append(HearingHistoryRow(hearing_date=hd, purpose=purpose, judge=before))

    title = f"{pet} vs {res}" if pet and res else (pet or res or ref)
    return Case(
        cnr=ref.strip(),
        title=title,
        court=court,
        stage=(d.get("Case Status") or "").strip() or None,
        next_hearing_date=parse_dmy(d.get("Next Listing Date")),
        judge=None,  # DRT names the Presiding Officer as "PO", not a person
        parties=parties,
        history=history,
        orders=[],
        filing_date=parse_dmy(d.get("Date of Filing")),
    )


@dataclass
class DRTClient:
    """``ForumAdapter`` for the DRT/DRAT tribunal kinds (Forum.TRIBUNAL)."""

    scope: str = "tribunal_drt"
    base_url: str = BASE_URL
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.TRIBUNAL,
        identifier_kind=IdentifierKind.TRIBUNAL_CASE_NO,
        supports_fetch=True,
        supports_search=False,
        supports_pdf=False,
        is_manual=False,
        tribunal_kind=TribunalKind.DRT,  # informational; the class also serves DRAT
    )
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA, "X-Requested-With": "XMLHttpRequest"})

    def _get(self, path: str, params: dict[str, str]) -> str:
        try:
            resp = self._http.get(f"{self.base_url}/{path}", params=params, timeout=_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"DRT connection error on {path}: {e}") from e
        if resp.status_code == 429:
            raise RateLimited(f"DRT returned 429 on {path}")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"DRT {resp.status_code} on {path}")
        return resp.text or ""

    def fetch_case(self, identifier: str) -> Case:
        sc, case_type, case_no, year = _split_identifier(identifier)
        search = self._get(
            "partyDetail.php",
            {"caseNo": case_no, "caseType": case_type, "year": year, "sc": sc, "id": "casetypewise"},
        )
        m = _B64_RE.search(search)
        if not m:
            raise CNRNotFound(cnr=f"drt:{sc}:{case_type}:{case_no}:{year}")
        detail = self._get("Misdetailreport.php", {"no": m.group(1)})
        return parse_detail_html(detail, sc=sc)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("DRT order-PDF fetch is a follow-up")
