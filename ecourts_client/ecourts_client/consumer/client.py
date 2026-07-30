"""Consumer forum (e-Jagriti / NCDRC-SCDRC-DCDRC) adapter.

Confirmed transport (see ``docs/spike-ejagriti-transport.md``): a public
plain-JSON REST API, commission-scoped 3-step flow —

  1. ``list_state_commissions()``    -> NCDRC + states / circuit benches
  2. ``list_district_commissions()`` -> resolve the leaf district commissionId
  3. ``search_by_case_number(...)``  -> read status/dates/parties within it
     (or ``search_by_name(...)`` when the exact case number isn't known)

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

# Name-based serchType values, for the interactive "I don't know the exact case
# number" path. See ``search_by_name``.
SEARCH_ROLES: dict[str, int] = {
    "complainant": 2,
    "respondent": 3,
    "complainant_advocate": 4,
    "respondent_advocate": 5,
}

# Default lookback for name search. Consumer matters run long — an NCDRC appeal
# against a 2016 complaint is routine — and the endpoint returns NOTHING outside
# the window rather than erroring, so a short default reads as "no such case".
_NAME_SEARCH_YEARS = 12

# --- NCDRC ------------------------------------------------------------------
# The apex National Commission. ``getStateCommissionAndCircuitBench`` enumerates
# ONLY State Commissions + circuit benches (54 rows, verified live 2026-07-30) —
# NCDRC is NOT among them, so a dropdown built purely from that lister can never
# offer it. e-Jagriti's own SPA works around this exactly the same way: it
# hardcodes ``{commissionName:"NCDRC", commissionId:11e6}`` as a client-side
# literal. 11000000 is live-verified against getCaseDetailsBySearchType, and its
# rows carry the identical schema to state/district rows (so ``parse_case`` and
# every downstream persistence/refresh path work unchanged).
#
# ⚠ This is an undocumented hardcoded constant on an unofficial NIC API. If NIC
# ever renumbers it, NCDRC lookups fail closed (CNRNotFound / empty list) rather
# than returning wrong data. tests/integration carries a canary that asserts the
# id still resolves — treat a sustained failure as the NIC-version-bump signal
# described in docs/spike-ejagriti-transport.md §7.
NCDRC_COMMISSION_ID = 11000000
NCDRC_COMMISSION = CommissionRef(
    commission_id=NCDRC_COMMISSION_ID,
    name="NCDRC (National Commission)",
    is_bench=False,
    active=True,
)

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
    neighbour. A ``rows[0]`` / substring fallback here would ship the wrong
    case's data under the caller's identifier (same-identity conflation).

    NOTE: an earlier version of this docstring claimed serchType=1 is a
    server-side SUBSTRING search. That is WRONG — re-verified live 2026-07-30,
    it matches the full case number only: for the real case ``NC/AE/10/2024``,
    both ``NC/AE/10`` and ``AE/10/2024`` returned 0 rows (control: Karnataka +
    ``"1"`` also 0). So upstream already returns 0-or-1 row and this check is
    normally a no-op. It is retained deliberately as a cheap invariant: it is
    the only thing standing between a silent upstream semantics change and
    persisting another party's case under this caller's identifier.
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
        """Top-of-cascade commissions: NCDRC + State Commissions + circuit benches.

        NCDRC is prepended because the upstream lister omits it entirely (see
        ``NCDRC_COMMISSION``); it leads the list because it is the apex forum.
        """
        return [NCDRC_COMMISSION, *parse_commissions(self._session.get(_EP_STATES))]

    def list_district_commissions(self, state_commission_id: int | str) -> list[CommissionRef]:
        """District (leaf) commissions under a state commission id.

        NCDRC short-circuits to ``[]`` — it is the apex commission and has no
        districts beneath it. That also avoids a pointless upstream round-trip:
        the endpoint never validates its input and answers ``[]`` for ANY id
        (verified 2026-07-30), so it cannot be used to probe id validity.
        """
        if str(state_commission_id) == str(NCDRC_COMMISSION_ID):
            return []
        data = self._session.get(
            _EP_DISTRICTS, params={"commissionId": str(state_commission_id)}
        )
        return parse_commissions(data)

    # --- search ----------------------------------------------------------
    def _search_rows(
        self,
        *,
        commission_id: int | str,
        search_value: str,
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
            "serchTypeValue": str(search_value),  # [sic]
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
            search_value=case_number,
            from_date=from_date,
            to_date=to_date,
            page=page,
            size=size,
        )
        return parse_case_stubs(rows)

    def search_by_name(
        self,
        *,
        commission_id: int | str,
        name: str,
        role: str = "complainant",
        from_date: date | str | None = None,
        to_date: date | str | None = None,
        page: int = 0,
        size: int = 30,
    ) -> list[CaseStub]:
        """Search a commission by party / advocate NAME → CaseStubs.

        The escape hatch for the exact-case-number requirement: ``serchType=1``
        resolves only a byte-exact case number (see ``_exact_match``), but
        e-Jagriti normalises numbers into its own form — an NCDRC complaint an
        advocate writes as "CC No. 743 of 2019" is stored as "NC/CC/743/2019",
        and nothing short of that string matches. Name search lets the caller
        offer a pick-list instead of demanding the portal's exact spelling.

        ``role`` selects the serchType (see ``SEARCH_ROLES``). Unlike case-number
        search this IS a loose match, so results are a candidate list for a human
        to choose from — never auto-resolve a single row into ``fetch_case``.

        The date window is REQUIRED by the endpoint and is NOT inferable from a
        name, so it defaults to a deliberately wide ``_NAME_SEARCH_YEARS``-year
        lookback. Narrow windows silently return nothing: "ANANT RAM" over
        2023–2024 gave 0 rows where 2015–2026 gave 3 (verified 2026-07-30).
        """
        try:
            serch_type = SEARCH_ROLES[role]
        except KeyError:
            raise ValueError(
                f"unknown role {role!r} (expected one of {sorted(SEARCH_ROLES)})"
            ) from None
        value = (name or "").strip()
        if not value:
            raise ValueError("name must be non-empty")
        ref = date.today()
        rows = self._search_rows(
            commission_id=commission_id,
            search_value=value,
            from_date=from_date or date(ref.year - _NAME_SEARCH_YEARS, 1, 1),
            to_date=to_date or ref,
            serch_type=serch_type,
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

        serchType=1 matches the full case number only (see ``_exact_match``), so
        in practice page 0 returns 0-or-1 row and this loop exits immediately on
        the short-page check. The paging is kept as cheap insurance in case some
        commission does match loosely — it costs nothing on the observed
        behaviour and is bounded by ``max_pages``."""
        for page in range(max_pages):
            rows = self._search_rows(
                commission_id=commission_id,
                search_value=case_number,
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
