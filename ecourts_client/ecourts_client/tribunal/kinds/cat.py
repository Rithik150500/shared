"""CAT (Central Administrative Tribunal) tribunal-kind adapter.

Verified transport (``docs/spike-tribunal-wave1.md``): the public
``cis.cgat.gov.in/catlive`` CIS, **no captcha**, DRT-shaped. Bench-first — 19
benches, each a numeric ``benchCode1`` (Delhi=100, Mumbai=210, Chennai=310, …;
the SELECT-BENCH menu sets it as ``atob(<base64>)``). Search is a single XHR:

  ``GET catlive/partyDetail.php?caseNo=<n>&benchCode1=<code>&caseType=<t>&year=<yyyy>&id=casetypewise``

→ an HTML result fragment: one row of [Diary No, Location, Case Type, Case No,
Date of Filing, Applicant, Respondent, (MORE DETAIL)]. Those give the core
``Case``; status / next-hearing / history live behind MORE DETAIL, whose detail
page needs server session state (stateless GET returns an empty page) — deferred.

Identifier: ``"<benchCode1>:<caseType>:<caseNo>:<year>"`` (e.g. ``"100:1:1:2023"``).
caseType: 1=Original Application, 2=Transfer Application, 3=Misc Application,
4=Contempt Petition, 5=Petition for Transfer, 6=Review Application,
7=Criminal Contempt Petition, 8=OA Obj.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

import requests
from bs4 import BeautifulSoup

from ecourts_client.errors import CNRNotFound, CourtSiteDown, ECourtsError, RateLimited
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind, TribunalKind
from ecourts_client.models import Case, Party
from ecourts_client.tribunal._html import parse_dmy

BASE_URL = "https://cis.cgat.gov.in/catlive"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TIMEOUT = 45
_DMY = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")  # a data row carries the filing date


def _split_identifier(identifier: str) -> tuple[str, str, str, str]:
    parts = (identifier or "").split(":")
    if len(parts) != 4 or not all(p.strip() for p in parts):
        raise ECourtsError(
            f"CAT identifier must be '<benchCode1>:<caseType>:<caseNo>:<year>', got {identifier!r}"
        )
    return tuple(p.strip() for p in parts)  # type: ignore[return-value]


def parse_search_html(html: str, *, bench_code: str) -> Case:
    """Parse the CAT ``partyDetail.php`` result fragment into a ``Case``. Raises
    ``CNRNotFound`` when there is no case row. Columns: [Diary No, Location, Case
    Type, Case No, Date of Filing, Applicant, Respondent, (MORE DETAIL)]."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[list[str]] = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        # a data row has the columns + a DD/MM/YYYY filing date (the header has none).
        if len(cells) >= 7 and _DMY.search(" ".join(cells)):
            rows.append(cells)
    if not rows:
        raise CNRNotFound(cnr=f"cat:{bench_code}")
    r = (rows[0] + [""] * 7)[:7]
    _diary, location, _ctype, case_no, filing, applicant, respondent = r

    parties: list[Party] = []
    if applicant:
        parties.append(Party(name=applicant, role="petitioner"))
    if respondent:
        parties.append(Party(name=respondent, role="respondent"))
    title = f"{applicant} vs {respondent}" if applicant and respondent else (applicant or respondent or case_no)

    return Case(
        cnr=case_no.strip(),
        title=title,
        court=f"Central Administrative Tribunal — {location}" if location else "Central Administrative Tribunal",
        stage=None,  # status/next-hearing/history are behind MORE DETAIL (deferred)
        next_hearing_date=None,
        judge=None,
        parties=parties,
        history=[],
        orders=[],
        filing_date=parse_dmy(filing),
    )


@dataclass
class CATClient:
    """``ForumAdapter`` for the CAT tribunal kind (Forum.TRIBUNAL / kind=CAT)."""

    scope: str = "tribunal_cat"
    base_url: str = BASE_URL
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.TRIBUNAL,
        identifier_kind=IdentifierKind.TRIBUNAL_CASE_NO,
        supports_fetch=True,
        supports_search=False,
        supports_pdf=False,
        is_manual=False,
        tribunal_kind=TribunalKind.CAT,
    )
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA, "X-Requested-With": "XMLHttpRequest"})

    def fetch_case(self, identifier: str) -> Case:
        bench, ctype, cno, year = _split_identifier(identifier)
        try:
            resp = self._http.get(
                f"{self.base_url}/partyDetail.php",
                params={"caseNo": cno, "benchCode1": bench, "caseType": ctype, "year": year, "id": "casetypewise"},
                timeout=_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"CAT connection error: {e}") from e
        if resp.status_code == 429:
            raise RateLimited("CAT returned 429")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"CAT {resp.status_code}")
        return parse_search_html(resp.text or "", bench_code=bench)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("CAT order fetch is a follow-up")
