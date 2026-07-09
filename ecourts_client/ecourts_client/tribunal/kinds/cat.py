"""CAT (Central Administrative Tribunal) tribunal-kind adapter.

Verified transport (``docs/spike-tribunal-wave1.md`` + live 2026-07-09): the
public ``cis.cgat.gov.in/catlive`` CIS, **no captcha**, DRT-shaped, 2-hop:

  1. ``GET catlive/partyDetail.php?caseNo=<n>&benchCode1=<code>&caseType=<t>&year=<yyyy>&id=casetypewise``
     → an HTML result fragment: one row of [Diary No, Location, Case Type, Case
     No, Date of Filing, Applicant, Respondent, MORE DETAIL] whose MORE DETAIL
     link is ``popsurety_detailreport('<b64>')``.
  2. ``GET catlive/Misdetailreport123.php?no=<b64>`` → the CASE STATUS detail
     fragment (2-cell label→value rows: Status / Stage, Date of Disposal,
     Petitioner(s)/Respondent(s), Subject, …). NOTE: the b64's ``home1.php``
     link is only the page *shell*; the data lives at ``Misdetailreport123.php``
     (CAT's analogue of DRT's ``Misdetailreport.php``), reachable as a plain XHR
     with the search cookie — no bench-session round-trip is needed.

Bench-first — 19 benches, each a numeric ``benchCode1`` (Delhi=100, Mumbai=210,
Chennai=310, …; the SELECT-BENCH menu sets it as ``atob(<base64>)``).

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
from ecourts_client.tribunal._html import label_value_map, parse_dmy

BASE_URL = "https://cis.cgat.gov.in/catlive"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TIMEOUT = 45
_DMY = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")  # a data row carries the filing date
_B64_RE = re.compile(r"popsurety_detailreport\(\s*['\"]([^'\"]+)['\"]\s*\)")
_FILING_RE = re.compile(r"Filing Date\s*:?\s*(\d{2}/\d{2}/\d{4})", re.I)
# CAT labels the current disposition "Status / Stage". A disposed case (live-
# verified) omits any next date; a pending case exposes it under one of these
# labels (inferred — extend when a live pending case is captured).
_NEXT_LABELS = ("Next Listing Date", "Next Date of Hearing", "Next Hearing Date", "Next Date")


def _split_identifier(identifier: str) -> tuple[str, str, str, str]:
    parts = (identifier or "").split(":")
    if len(parts) != 4 or not all(p.strip() for p in parts):
        raise ECourtsError(
            f"CAT identifier must be '<benchCode1>:<caseType>:<caseNo>:<year>', got {identifier!r}"
        )
    return tuple(p.strip() for p in parts)  # type: ignore[return-value]


def _clean_party(raw: str | None) -> str:
    """First meaningful party name from CAT's messy ``NAME (M) , OTHER ,`` cell:
    take the first comma-segment, drop the ``(M)``/``(F)`` gender marker, collapse."""
    if not raw:
        return ""
    first = raw.split(",")[0]
    first = re.sub(r"\((?:M|F)\)", "", first)
    return re.sub(r"\s+", " ", first).strip()


def parse_search_html(html: str, *, bench_code: str) -> Case:
    """Parse the CAT ``partyDetail.php`` result fragment into a ``Case``. Raises
    ``CNRNotFound`` when there is no case row. Columns: [Diary No, Location, Case
    Type, Case No, Date of Filing, Applicant, Respondent, (MORE DETAIL)].

    Returns the core row only (``stage``/``next_hearing_date`` are ``None`` here);
    ``fetch_case`` follows the MORE DETAIL link for the live status."""
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
        stage=None,  # status/next-hearing live behind MORE DETAIL (see parse_detail_html)
        next_hearing_date=None,
        judge=None,
        parties=parties,
        history=[],
        orders=[],
        filing_date=parse_dmy(filing),
    )


def parse_detail_html(html: str, *, bench_code: str) -> Case:
    """Parse the CAT ``Misdetailreport123.php`` CASE STATUS page into a ``Case``.

    2-cell label→value rows (live-verified 2026-07-09): ``Location``,
    ``Case Number``, ``Status / Stage``, ``Disposal Nature``, ``Date of Disposal``,
    ``Petitioner(s)``/``Respondent(s)``, ``Subject``. The filing date sits in the
    header text (``Filing Date : DD/MM/YYYY``), not a label row. Raises
    ``CNRNotFound`` when the page carries no ``Case Number`` row.

    A disposed case (``Status / Stage`` contains DISPOSED) has no next date; a
    pending case exposes it under one of ``_NEXT_LABELS``."""
    soup = BeautifulSoup(html, "html.parser")
    d = label_value_map(soup)
    ref = d.get("Case Number")
    if not ref:
        raise CNRNotFound(cnr=f"cat:{bench_code}")

    stage = (d.get("Status / Stage") or "").strip() or None
    disposed = bool(stage and "DISPOSED" in stage.upper())
    next_hd = None
    if not disposed:
        for lbl in _NEXT_LABELS:
            next_hd = parse_dmy(d.get(lbl))
            if next_hd:
                break

    pet = _clean_party(d.get("Petitioner(s)"))
    res = _clean_party(d.get("Respondent(s)"))
    parties: list[Party] = []
    if pet:
        parties.append(Party(name=pet, role="petitioner"))
    if res:
        parties.append(Party(name=res, role="respondent"))

    location = d.get("Location")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    fm = _FILING_RE.search(text)
    title = f"{pet} vs {res}" if pet and res else (pet or res or ref.strip())

    return Case(
        cnr=ref.strip(),
        title=title,
        court=f"Central Administrative Tribunal — {location}" if location else "Central Administrative Tribunal",
        stage=stage,
        next_hearing_date=next_hd,
        judge=None,
        parties=parties,
        history=[],
        orders=[],
        filing_date=parse_dmy(fm.group(1)) if fm else None,
    )


@dataclass
class CATClient:
    """``ForumAdapter`` for the CAT tribunal kind (Forum.TRIBUNAL / kind=CAT).

    ``fetch_case`` is a session-aware 2-hop (search → MORE DETAIL data page); the
    detail fetch is internal, so callers see one call → one complete ``Case``."""

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

    def _get(self, path: str, params: dict[str, str]) -> str:
        try:
            resp = self._http.get(f"{self.base_url}/{path}", params=params, timeout=_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"CAT connection error on {path}: {e}") from e
        if resp.status_code == 429:
            raise RateLimited(f"CAT returned 429 on {path}")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"CAT {resp.status_code} on {path}")
        return resp.text or ""

    def fetch_case(self, identifier: str) -> Case:
        bench, ctype, cno, year = _split_identifier(identifier)
        # Hop 1: the party/case-type search XHR → the row + MORE DETAIL b64 link.
        search = self._get(
            "partyDetail.php",
            {"caseNo": cno, "benchCode1": bench, "caseType": ctype, "year": year, "id": "casetypewise"},
        )
        m = _B64_RE.search(search)
        if not m:
            raise CNRNotFound(cnr=f"cat:{bench}:{ctype}:{cno}:{year}")
        # Hop 2: the CASE STATUS data page (session cookie from hop 1 is reused).
        detail = self._get("Misdetailreport123.php", {"no": m.group(1)})
        return parse_detail_html(detail, bench_code=bench)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("CAT order fetch is a follow-up")
