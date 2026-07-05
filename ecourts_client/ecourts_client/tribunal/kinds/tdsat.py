"""TDSAT (Telecom Disputes Settlement & Appellate Tribunal) tribunal-kind adapter.

Verified transport (``docs/spike-tribunal-transport.md``): single New Delhi bench,
plain PHP, no captcha, ONE hop —
``POST tdsat.gov.in/Delhi/services/checkhomedetail1.php``
(``pet_type=1&casetype=<code>&caseno=<n>&caseyear=<yyyy>&submit1=Search``) returns
a rich CASE STATUS page (label→value rows + party blocks + a proceeding table).

Identifier: ``"<casetype>:<caseno>:<caseyear>"`` (e.g. ``"2:1:2023"``; casetype is
the numeric TDSAT code — 2=Telecom Petition, 4=Telecom Appeal, …).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

import requests

from ecourts_client.errors import CNRNotFound, CourtSiteDown, ECourtsError, RateLimited
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind, TribunalKind
from ecourts_client.models import Case, HearingHistoryRow, Party
from ecourts_client.tribunal._html import (
    extract_after_dash,
    label_value_map,
    parse_dmy,
    proceeding_table_rows,
)
from bs4 import BeautifulSoup

BASE_URL = "https://tdsat.gov.in/Delhi/services"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TIMEOUT = 40


def _split_identifier(identifier: str) -> tuple[str, str, str]:
    parts = (identifier or "").split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ECourtsError(f"TDSAT identifier must be '<casetype>:<caseno>:<caseyear>', got {identifier!r}")
    return tuple(p.strip() for p in parts)  # type: ignore[return-value]


def _adv(text: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*:?-?\s*(.+?)\s*(?:Additional|Respondent|Petitioner|$)", text, re.I | re.S)
    if not m:
        return None
    v = re.sub(r"\s+", " ", m.group(1)).strip(" -,:")
    return v or None


def parse_status_html(html: str, *, casetype: str, caseno: str, caseyear: str) -> Case:
    """Parse the TDSAT CASE STATUS page into a ``Case``. Raises ``CNRNotFound``
    when the page carries no case (unknown {casetype,caseno,caseyear})."""
    soup = BeautifulSoup(html, "html.parser")
    d = label_value_map(soup)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    ref = d.get("Case Type/Case No/Year")
    if not ref and "Case Status" not in d:
        raise CNRNotFound(cnr=f"tdsat:{casetype}:{caseno}:{caseyear}")

    pet = extract_after_dash(text, "Petitioner Name")
    res = extract_after_dash(text, "Respondent Name")
    parties: list[Party] = []
    if pet:
        parties.append(Party(name=pet, role="petitioner", advocate=_adv(text, "Pet. Advocate Name")))
    if res:
        parties.append(Party(name=res, role="respondent", advocate=_adv(text, "Respondent Advocate")))

    status = d.get("Case Status") or ""
    disposal = d.get("Disposal Nature")
    stage = status + (f" — {disposal}" if disposal and status.lower().startswith("dispos") else "")

    history: list[HearingHistoryRow] = []
    for row in proceeding_table_rows(soup, "Hearing Date", 5):
        # [Bench No, Hearing Date, Purpose, Status, Order]
        hd = parse_dmy(row[1])
        if hd:
            history.append(HearingHistoryRow(hearing_date=hd, purpose=row[2], judge=""))

    title = f"{pet} vs {res}" if pet and res else (pet or res or ref or f"{casetype}/{caseno}/{caseyear}")
    return Case(
        cnr=(ref or f"{casetype}/{caseno}/{caseyear}").replace(" ", " ").strip(),
        title=title,
        court="Telecom Disputes Settlement and Appellate Tribunal, New Delhi",
        stage=stage or None,
        next_hearing_date=None,  # TDSAT status page exposes no clean 'next date'
        judge=None,
        parties=parties,
        history=history,
        orders=[],
        filing_date=parse_dmy(d.get("Date of Filing")),
    )


@dataclass
class TDSATClient:
    """``ForumAdapter`` for the TDSAT tribunal kind (Forum.TRIBUNAL / kind=TDSAT)."""

    scope: str = "tribunal_tdsat"
    base_url: str = BASE_URL
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.TRIBUNAL,
        identifier_kind=IdentifierKind.TRIBUNAL_CASE_NO,
        supports_fetch=True,
        supports_search=False,
        supports_pdf=False,
        is_manual=False,
        tribunal_kind=TribunalKind.TDSAT,
    )
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA})

    def fetch_case(self, identifier: str) -> Case:
        casetype, caseno, caseyear = _split_identifier(identifier)
        try:
            resp = self._http.post(
                f"{self.base_url}/checkhomedetail1.php",
                data={"pet_type": "1", "casetype": casetype, "caseno": caseno, "caseyear": caseyear, "submit1": "Search"},
                timeout=_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"TDSAT connection error: {e}") from e
        if resp.status_code == 429:
            raise RateLimited("TDSAT returned 429")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"TDSAT {resp.status_code}")
        return parse_status_html(resp.text or "", casetype=casetype, caseno=caseno, caseyear=caseyear)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("TDSAT order-PDF fetch is a follow-up (daily_order_view.php)")
