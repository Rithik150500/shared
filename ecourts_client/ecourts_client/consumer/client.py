"""Consumer forum (e-Jagriti / NCDRC-SCDRC-DCDRC) adapter.

Confirmed transport (see ``docs/spike-ejagriti-transport.md``): a public
plain-JSON REST API, commission-scoped 3-step flow —

  1. ``list_state_commissions()``    -> enumerate states / circuit benches
  2. ``list_district_commissions()`` -> resolve the leaf district commissionId
  3. ``search_by_case_number(...)``  -> read status/dates/parties within it

There is NO global CNR-style key: the Consumer identity is the e-Jagriti case
number scoped to a commission + a date window. To satisfy the ``ForumAdapter``
``fetch_case(identifier: str)`` contract, the identifier is the COMPOSITE
``"<commissionId>:<caseNumber>"``; the filing-year suffix of the case number
seeds the search date window so a bare (commission, caseNumber) pair suffices.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

from ecourts_client.consumer._session import ConsumerSession
from ecourts_client.consumer.models import CommissionRef
from ecourts_client.consumer.parsers import (
    parse_case,
    parse_case_stubs,
    parse_commissions,
)
from ecourts_client.errors import CNRNotFound, IdentifierMalformed, SchemaChanged
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind
from ecourts_client.models import Case, CaseStub

# --- e-Jagriti endpoints (relative to ConsumerSession.base_url) -------------
_EP_STATES = "report/report/getStateCommissionAndCircuitBench"
_EP_DISTRICTS = "report/report/getDistrictCommissionByCommissionId"
_EP_SEARCH = "case/caseFilingService/v2/getCaseDetailsBySearchType"

# serchType / dateRequestType enums (server misspelling of "search" kept verbatim
# in the request body — do NOT "fix" it).
_SEARCH_BY_CASE_NUMBER = 1
_DATE_BY_FILING = 1

# e-Jagriti case numbers end with the 4-digit filing year (e.g. SC/29/A/1006/2024).
_YEAR_RE = re.compile(r"(\d{4})\s*$")


def _split_identifier(identifier: str) -> tuple[int, str]:
    """Parse the composite ``"<commissionId>:<caseNumber>"`` fetch identifier."""
    if not identifier or ":" not in identifier:
        raise IdentifierMalformed(
            forum=Forum.CONSUMER.value,
            identifier=identifier,
            reason="expected '<commissionId>:<caseNumber>'",
        )
    cid, _, case_no = identifier.partition(":")
    cid, case_no = cid.strip(), case_no.strip()
    if not cid.isdigit() or not case_no:
        raise IdentifierMalformed(
            forum=Forum.CONSUMER.value,
            identifier=identifier,
            reason="commissionId must be numeric and caseNumber non-empty",
        )
    return int(cid), case_no


def _window_for_case_number(case_number: str, *, today: date | None = None) -> tuple[date, date]:
    """Derive a filing-date search window from the case number's year suffix.

    dateRequestType=1 searches by FILING date, so a window bracketing the filing
    year finds the case. Falls back to the last ~3 years when no year is parsable.
    """
    ref = today or date.today()
    m = _YEAR_RE.search(case_number)
    if m:
        yr = int(m.group(1))
        if 1990 <= yr <= ref.year + 1:  # +1: a late-year filing can carry next FY
            from_yr = min(yr, ref.year)
            to = ref if yr >= ref.year else date(yr, 12, 31)
            return date(from_yr, 1, 1), to
    return date(ref.year - 3, 1, 1), ref


def _exact_match(rows: list[dict[str, Any]], case_number: str) -> dict[str, Any] | None:
    """Return the row whose caseNumber EXACTLY equals ``case_number`` (normalized).

    fetch_case MUST resolve to the requested case or fail — never substitute a
    neighbour. serchType=1 is a server-side SUBSTRING search over a whole
    commission+window, so the returned rows routinely include OTHER cases; a
    ``rows[0]`` / substring fallback here would ship the wrong case's data under
    the caller's identifier (same-identity conflation). Loose/substring matching
    belongs ONLY in the interactive ``search_by_case_number`` list path.
    """
    cn = case_number.strip().lower()
    for row in rows:
        if str(row.get("caseNumber") or "").strip().lower() == cn:
            return row
    return None


@dataclass
class ConsumerClient:
    """``ForumAdapter`` for the Consumer forum (e-Jagriti)."""

    scope: str = "consumer"
    # Multi-forum adapter contract. ClassVar so it isn't a dataclass field.
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.CONSUMER,
        identifier_kind=IdentifierKind.EJAGRITI_CASE_NO,
        supports_fetch=True,
        supports_search=True,
        supports_pdf=True,
        is_manual=False,
    )
    _session: ConsumerSession = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = ConsumerSession()

    # --- commission resolution -------------------------------------------
    def list_state_commissions(self) -> list[CommissionRef]:
        """State Commissions + circuit benches (top of the resolve chain)."""
        return parse_commissions(self._session.get(_EP_STATES))

    def list_district_commissions(self, state_commission_id: int | str) -> list[CommissionRef]:
        """District (leaf) commissions under a state commission id."""
        data = self._session.get(
            _EP_DISTRICTS, params={"commissionId": str(state_commission_id)}
        )
        return parse_commissions(data)

    # --- search ----------------------------------------------------------
    def _search_rows(
        self,
        *,
        commission_id: int | str,
        case_number: str,
        from_date: date | str,
        to_date: date | str,
        date_request_type: int = _DATE_BY_FILING,
        serch_type: int = _SEARCH_BY_CASE_NUMBER,
        page: int = 0,
        size: int = 30,
    ) -> list[dict[str, Any]]:
        body = {
            "commissionId": int(commission_id),
            "dateRequestType": date_request_type,
            "fromDate": from_date.isoformat() if isinstance(from_date, date) else str(from_date),
            "toDate": to_date.isoformat() if isinstance(to_date, date) else str(to_date),
            "judgeId": "",
            "page": page,
            "size": size,
            "serchType": serch_type,          # [sic] NIC misspelling — keep verbatim
            "serchTypeValue": str(case_number),  # [sic]
        }
        data = self._session.post(_EP_SEARCH, body=body)
        if data is None:
            return []  # no results (envelope data:null)
        if not isinstance(data, list):
            raise SchemaChanged(
                "data", f"case-search payload not a list: {type(data).__name__}"
            )
        return data

    def search_by_case_number(
        self,
        *,
        commission_id: int | str,
        case_number: str,
        from_date: date | str,
        to_date: date | str,
        page: int = 0,
        size: int = 30,
    ) -> list[CaseStub]:
        """Search a commission by case number over a date window → CaseStubs."""
        rows = self._search_rows(
            commission_id=commission_id,
            case_number=case_number,
            from_date=from_date,
            to_date=to_date,
            page=page,
            size=size,
        )
        return parse_case_stubs(rows)

    # --- ForumAdapter contract -------------------------------------------
    def _find_exact(
        self,
        commission_id: int,
        case_number: str,
        from_date: date,
        to_date: date,
        *,
        size: int = 50,
        max_pages: int = 5,
    ) -> dict[str, Any] | None:
        """Page the commission+window search for an EXACT case-number match.

        serchType=1 is a substring search, so the exact case can sit past page 0
        among unrelated rows; page forward until found or the results run short
        (bounded by ``max_pages`` so a common substring can't loop forever)."""
        for page in range(max_pages):
            rows = self._search_rows(
                commission_id=commission_id,
                case_number=case_number,
                from_date=from_date,
                to_date=to_date,
                page=page,
                size=size,
            )
            match = _exact_match(rows, case_number)
            if match is not None:
                return match
            if len(rows) < size:  # short page => no more results
                break
        return None

    def fetch_case(self, identifier: str) -> Case:
        """Fetch one case by the composite ``"<commissionId>:<caseNumber>"``.

        Resolves to the EXACT requested case or raises ``CNRNotFound`` — never a
        substring/neighbour guess (see ``_exact_match``)."""
        commission_id, case_number = _split_identifier(identifier)
        from_date, to_date = _window_for_case_number(case_number)
        match = self._find_exact(commission_id, case_number, from_date, to_date)
        if match is None:
            raise CNRNotFound(cnr=case_number)
        return parse_case(match)

    def fetch_pdf(self, url: str) -> bytes:
        return self._session.fetch_pdf(url)
