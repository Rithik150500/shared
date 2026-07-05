"""NCLAT (National Company Law Appellate Tribunal) tribunal-kind adapter.

Verified transport (``docs/spike-tribunal-transport.md``): a Laravel app at
``nclat.nic.in``, no captcha, two-hop:

  1. ``GET  /display-board/cases``          -> scrape CSRF ``_token`` + session cookie
  2. ``POST /display-board/cases_details``  -> DataTables JSON; ``data[0][1]`` = filing_no
  3. ``POST /display-board/view_details``   -> full case JSON (parties/hearings/orders)

``ForumAdapter.fetch_case`` identifier is the composite
``"<location>:<case_type>:<case_number>:<case_year>"`` (e.g. ``"delhi:33:1:2023"``);
``location`` in {delhi, chennai}, ``case_type`` the numeric NCLAT code
(33 = Company Appeal(AT)(Ins), …). Search-by-party + order-PDF fetch are
follow-ups (``supports_search``/``supports_pdf`` = False).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar

import requests

from ecourts_client.errors import CNRNotFound, CourtSiteDown, ECourtsError, RateLimited
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind, TribunalKind
from ecourts_client.models import Case, HearingHistoryRow, OrderRef, Party

BASE_URL = "https://nclat.nic.in"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 40
# location code -> human bench label
_BENCH = {"delhi": "New Delhi", "chennai": "Chennai"}
_TOKEN_RE = re.compile(r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', re.I)


def _split_identifier(identifier: str) -> tuple[str, str, str, str]:
    """Parse ``"<location>:<case_type>:<case_number>:<case_year>"``."""
    parts = (identifier or "").split(":")
    if len(parts) != 4 or not all(p.strip() for p in parts):
        raise ECourtsError(
            f"NCLAT identifier must be '<location>:<case_type>:<case_number>:<case_year>', got {identifier!r}"
        )
    loc, ctype, cno, cyr = (p.strip() for p in parts)
    if loc not in _BENCH:
        raise ECourtsError(f"NCLAT location must be one of {sorted(_BENCH)}, got {loc!r}")
    return loc, ctype, cno, cyr


def _pdate(s: Any) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` string; None on empty/garbage."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().strip(",").strip()


def _names(x: Any) -> list[str]:
    """Normalise a party/advocate value that may be a list of {name}/str, or a str."""
    out: list[str] = []
    if isinstance(x, list):
        for item in x:
            n = item.get("name") if isinstance(item, dict) else item
            if n and _clean(n):
                out.append(_clean(n))
    elif x and _clean(x):
        out.append(_clean(x))
    return out


def _title_from(parties: list[str], others: list[str]) -> str:
    a = (parties[0] + (" & Ors." if len(parties) > 1 else "")) if parties else ""
    b = (others[0] + (" & Ors." if len(others) > 1 else "")) if others else ""
    if a and b:
        return f"{a} vs {b}"
    return a or b or ""


def parse_view_details(data: dict[str, Any], *, location: str) -> Case:
    """Map the ``view_details`` ``data`` object into a generic ``Case``.

    Pure (no I/O) so it can be unit-tested against a captured fixture. Raises
    ``CNRNotFound`` when the detail payload carries no case row.
    """
    cds = data.get("case_details") or []
    if not cds:
        raise CNRNotFound(cnr="nclat")
    cd = cds[0]
    case_no = _clean(cd.get("case_no"))
    case_year = _clean(cd.get("case_year"))
    case_type = _clean(cd.get("case_type"))
    status = _clean(cd.get("status")).upper()
    # Human, stable per-forum ref (unique within a user's NCLAT cases).
    ref = f"{case_type} {case_no}/{case_year}".strip()

    pd = data.get("party_details") or {}
    applicants = _names(pd.get("applicant_name"))
    respondents = _names(pd.get("respondant_name"))
    lr = data.get("legal_representative") or {}
    app_adv = ", ".join(_names(lr.get("applicant_legal_representative_name"))) or None
    res_adv = ", ".join(_names(lr.get("respondent_legal_representative_name"))) or None

    parties: list[Party] = []
    for i, n in enumerate(applicants):
        parties.append(Party(name=n, role="petitioner", advocate=app_adv if i == 0 else None))
    for i, n in enumerate(respondents):
        parties.append(Party(name=n, role="respondent", advocate=res_adv if i == 0 else None))

    nxt = data.get("next_hearing_details") or {}
    coram = _clean(nxt.get("coram")) or None
    stage_of_case = _clean(nxt.get("stage_of_case"))
    status_label = {"D": "Disposed", "P": "Pending"}.get(status, status or "")
    stage = stage_of_case or status_label

    history: list[HearingHistoryRow] = []
    for row in data.get("case_history") or []:
        hd = _pdate(row.get("hearing_date"))
        if hd:
            history.append(
                HearingHistoryRow(hearing_date=hd, purpose=_clean(row.get("purpose")), judge=coram or "")
            )

    orders: list[OrderRef] = []
    for row in data.get("order_history") or []:
        od = _pdate(row.get("order_date"))
        path = _clean(row.get("order_pdf_download"))
        if od and path:
            url = path if path.startswith("http") else BASE_URL + path
            orders.append(OrderRef(order_date=od, order_url=url, order_id=os.path.basename(path)))

    return Case(
        cnr=ref,
        title=_title_from(applicants, respondents) or ref,
        court=f"National Company Law Appellate Tribunal, {_BENCH.get(location, location)}",
        stage=stage,
        next_hearing_date=_pdate(nxt.get("hearing_date")),
        judge=coram,
        parties=parties,
        history=history,
        orders=orders,
        filing_date=_pdate(cd.get("date_of_filing")) or _pdate(cd.get("registration_date")),
    )


@dataclass
class NCLATClient:
    """``ForumAdapter`` for the NCLAT tribunal kind (Forum.TRIBUNAL / kind=NCLAT)."""

    scope: str = "tribunal_nclat"
    base_url: str = BASE_URL
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.TRIBUNAL,
        identifier_kind=IdentifierKind.TRIBUNAL_CASE_NO,
        supports_fetch=True,
        supports_search=False,
        supports_pdf=False,
        is_manual=False,
        tribunal_kind=TribunalKind.NCLAT,
    )
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA, "Accept": "text/html,application/json,*/*"})

    # --- transport (split out so fetch_case orchestration is unit-testable) ---
    def _open_session(self) -> str:
        """GET the search page → scrape the Laravel CSRF ``_token`` (session cookie
        lands in the shared cookie jar). Raises CourtSiteDown on transport/shape."""
        try:
            resp = self._http.get(f"{self.base_url}/display-board/cases", timeout=_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"NCLAT connection error: {e}") from e
        if resp.status_code == 429:
            raise RateLimited("NCLAT returned 429 opening the search page")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"NCLAT {resp.status_code} opening the search page")
        m = _TOKEN_RE.search(resp.text or "")
        if not m:
            raise CourtSiteDown("NCLAT: could not scrape csrf-token (page shape changed)")
        return m.group(1)

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            resp = self._http.post(
                f"{self.base_url}/{path}",
                data=payload,
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"NCLAT connection error on {path}: {e}") from e
        if resp.status_code == 429:
            raise RateLimited(f"NCLAT returned 429 on {path}")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"NCLAT {resp.status_code} on {path}")
        try:
            return resp.json()
        except ValueError as e:
            raise CourtSiteDown(f"NCLAT non-JSON response on {path}") from e

    def _search_filing_no(self, token: str, loc: str, ctype: str, cno: str, cyr: str) -> str:
        """POST cases_details → the internal filing_no (data[0][1]). CNRNotFound if empty."""
        payload = {
            "_token": token, "search_by": "case_no_wise", "location": loc,
            "case_type": ctype, "case_number": cno, "case_year": cyr,
            "exact_search_word": "1", "case_status": "all", "select_party": "",
            "party_name": "", "diary_no": "", "advocate_name": "", "text_name": "",
            "from_date": "", "to_date": "",
        }
        rows = (self._post("display-board/cases_details", payload) or {}).get("data") or []
        if not rows or len(rows[0]) < 2 or not rows[0][1]:
            raise CNRNotFound(cnr=f"nclat:{loc}:{ctype}:{cno}:{cyr}")
        return str(rows[0][1])

    def fetch_case(self, identifier: str) -> Case:
        """Fetch an NCLAT case by ``"<location>:<case_type>:<case_number>:<case_year>"``."""
        loc, ctype, cno, cyr = _split_identifier(identifier)
        token = self._open_session()
        filing_no = self._search_filing_no(token, loc, ctype, cno, cyr)
        detail = self._post(
            "display-board/view_details",
            {"search_type": "view_details", "filing_no": filing_no, "bench_name": loc, "_token": token},
        )
        data = (detail or {}).get("data")
        if not data:
            raise CNRNotFound(cnr=f"nclat:{loc}:{ctype}:{cno}:{cyr}")
        return parse_view_details(data, location=loc)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("NCLAT order-PDF fetch is a follow-up")
