"""High court client. Single-step CNR resolution:

    caseHistoryWebService.php with {cino: <CNR>}
    -> JSON-with-history dict
    -> parse_case_history -> Case

Per index_hc.js:455, the HC payload is just {cino: ...} -- no version_number,
language_flag, or bilingual_flag fields. Otherwise the request envelope and
response shape match district court.

Search-mode endpoints are reused from the DC stack (showDataWebService.php,
caseNumberSearch.php) but routed through the HC base URL by the HC session.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

from ecourts_client._session import Session
from ecourts_client.errors import CNRNotFound
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind
from ecourts_client.models import (
    BenchRef,
    Case,
    CaseStub,
    CaseTypeRef,
    DailyBusiness,
    HCBenchSitting,
    HCCauseListIndex,
    HCCauseListPDFRow,
    StateRef,
)
from ecourts_client.parsers.case_history import parse_case_history
from ecourts_client.parsers.cause_list_hc import (
    parse_hc_bench_sittings,
    parse_hc_cause_list_index,
)
from ecourts_client.parsers.cause_list_hc_pdf import parse_hc_cause_list_pdf
from ecourts_client.parsers.daily_business import parse_daily_business
from ecourts_client.parsers.dropdowns import parse_states
from ecourts_client.parsers.dropdowns_extra import parse_case_types, parse_hc_benches
from ecourts_client.parsers.search import parse_case_number_search, parse_party_search
from ecourts_client.pdf import fetch_pdf


@dataclass
class HighCourtClient:
    scope: str = "highcourt"
    # Multi-forum adapter contract (see forums.ForumAdapter). ClassVar so it
    # isn't a dataclass field; fetch_case/fetch_pdf below satisfy the Protocol.
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.ECOURTS_HIGHCOURT,
        identifier_kind=IdentifierKind.CNR,
        supports_fetch=True,
        supports_search=True,
        supports_pdf=True,
        is_manual=False,
    )
    _session: Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = Session(scope="highcourt")

    def fetch_case(self, cnr: str) -> Case:
        response = self._session.call("caseHistoryWebService.php", {"cino": cnr})

        if not response.get("history"):
            raise CNRNotFound(cnr=cnr)

        return parse_case_history(response, cnr=cnr)

    def fetch_pdf(self, url: str) -> bytes:
        return fetch_pdf(self._session._http, url)

    def list_states(self) -> list[StateRef]:
        resp = self._session.call(
            "stateWebService.php",
            {"action_code": "fillState", "time": str(time.time())},
        )
        return parse_states(resp)

    def list_bench_sittings(
        self, *, state_code: str, district_code: str, court_code: str, sitting_date: date
    ) -> list[HCBenchSitting]:
        """Return the benches sitting on a given date (causeListBenchWebService.php).

        Empty list on holidays / non-sitting days.
        """
        resp = self._session.call(
            "causeListBenchWebService.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "court_code": str(court_code),
                "date": sitting_date.strftime("%d-%m-%Y"),
            },
        )
        return parse_hc_bench_sittings(resp, state_code=str(state_code), sitting_date=sitting_date)

    def fetch_cause_list_index(
        self,
        *,
        state_code: str,
        district_code: str,
        court_code: str,
        bench_id: str,
        list_date: date,
        today: date | None = None,
    ) -> list[HCCauseListIndex]:
        """Return the cause-list INDEX for a bench on a date.

        Each entry points to a downloadable PDF (via pdf_url). Actual case-row
        extraction from those PDFs is deferred -- see docs/DEFERRED.md for the
        pdfplumber-tuning plan.
        """
        ref_today = today or date.today()
        sel_prev_days = "1" if list_date < ref_today else "0"
        resp = self._session.call(
            "cases_new.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "selprevdays": sel_prev_days,
                "court_code": str(court_code),
                "causelist_date": list_date.strftime("%d-%m-%Y"),
                "bench_id": str(bench_id),
            },
        )
        return parse_hc_cause_list_index(resp)

    def fetch_cause_list_pdf_rows(self, *, pdf_url: str) -> list[HCCauseListPDFRow]:
        """Download an HC cause-list PDF and extract its rows.

        Position-based extraction is heuristic; raw_text on each row is the
        canonical record, structured columns (case_number) are best-effort.
        Parties/advocates are left empty -- per-bench tuning needed for splits.
        """
        pdf_bytes = fetch_pdf(self._session._http, pdf_url)
        return parse_hc_cause_list_pdf(pdf_bytes)

    def list_hc_benches(self, state_code: str) -> list[BenchRef]:
        """HC bench list. Reuses districtWebService.php with action_code='benches' --
        the response key is still `districts` but the rows are benches."""
        resp = self._session.call(
            "districtWebService.php",
            {"state_code": str(state_code), "test_param": "pending", "action_code": "benches"},
        )
        return parse_hc_benches(resp, state_code=str(state_code))

    def list_case_types(
        self, *, state_code: str, district_code: str = "1", court_code: str = "1"
    ) -> list[CaseTypeRef]:
        """List case-type codes for an HC establishment.

        HC reuses `caseNumberWebService.php` (same shape as district court) but
        served from the HC base URL. For HC scope `district_code` and
        `court_code` are typically '1'/'1' -- the same convention used by
        `list_bench_sittings`.

        Used by `bot.causelist.case_type_cache` to build the abbrev -> numeric
        code map that the CNR back-resolver needs.
        """
        resp = self._session.call(
            "caseNumberWebService.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "court_code": str(court_code),
                "language_flag": "english",
                "bilingual_flag": "0",
            },
        )
        return parse_case_types(resp, court_code=str(court_code))

    def search_by_party_name(
        self,
        *,
        state_code: str,
        bench_code: str,
        party_name: str,
        year: int,
        pending_disposed: str = "Pending",
    ) -> list[CaseStub]:
        """Party-name search on a High Court bench.

        Unlike the District Court (which sends the establishment CSV in
        ``court_code_arr`` plus language flags -- main.js:displayCasesTable),
        the HC app selects a bench that sets BOTH ``dist_code`` and
        ``court_code`` to the bench code and sends NO language flags
        (search_by_party_name_hc.js + main_hc.js:displayCasesTable). Sending
        the DC shape makes showDataWebService.php return ``{status:"N",
        msg:"error"}``. ``bench_code`` is the dist_code returned by
        ``list_hc_benches`` for the picked High Court.
        """
        if pending_disposed not in {"Pending", "Disposed", "Both"}:
            raise ValueError(f"pending_disposed must be Pending|Disposed|Both, got {pending_disposed!r}")
        resp = self._session.call(
            "showDataWebService.php",
            {
                "state_code": str(state_code),
                "dist_code": str(bench_code),
                "court_code": str(bench_code),
                "pet_name": party_name,
                "pendingDisposed": pending_disposed,
                "year": str(year),
            },
        )
        return parse_party_search(resp)

    def search_by_case_number(
        self,
        *,
        state_code: str,
        bench_code: str,
        case_type: str,
        case_number: str,
        year: int,
    ) -> list[CaseStub]:
        """Case-number search on a High Court bench. See ``search_by_party_name``
        for why the HC envelope differs from District Court (court_code
        singular = dist_code = bench, no language flags)."""
        resp = self._session.call(
            "caseNumberSearch.php",
            {
                "state_code": str(state_code),
                "dist_code": str(bench_code),
                "court_code": str(bench_code),
                "case_number": str(case_number),
                "case_type": str(case_type),
                "year": str(year),
            },
        )
        return parse_case_number_search(resp)

    def fetch_daily_business(
        self,
        *,
        cnr: str,
        case_number: str,
        court_code: str,
        court_no: str,
        district_code: str,
        state_code: str,
        business_date: date,
        next_hearing_date: date,
        disposal_flag: str = "Pending",
    ) -> DailyBusiness:
        resp = self._session.call(
            "s_show_business.php",
            {
                "court_code": str(court_code),
                "dist_code": str(district_code),
                "nextdate1": next_hearing_date.strftime("%Y%m%d"),
                "case_number1": str(case_number),
                "state_code": str(state_code),
                "disposal_flag": disposal_flag,
                "businessDate": business_date.strftime("%d-%m-%Y"),
                "court_no": str(court_no),
                "language_flag": "english",
                "bilingual_flag": "0",
            },
        )
        return parse_daily_business(resp, cnr=cnr, business_date=business_date)
