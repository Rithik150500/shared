"""District court client. Two-step CNR resolution:

    1. listOfCasesWebService.php with {cino, version_number, language_flag, bilingual_flag}
       -> {case_number: <ID> | None}
    2a. If case_number is non-null: caseHistoryWebService.php with {cinum, language_flag, bilingual_flag}
        (note: cinum, not cino)
    2b. If case_number is null (filing case): filingCaseHistory.php with {cino, language_flag, bilingual_flag}

    Both 2a and 2b return a JSON-with-history dict that parses identically.

Per index.js:497-563.

Search-mode endpoints follow the JS app's `displayCasesTable` pattern (main.js:13198+):
the per-search request_data (e.g. `{pet_name, pendingDisposed, year}`) is merged with a
scope envelope `{state_code, dist_code, court_code_arr, language_flag, bilingual_flag}`
sourced from the user's selected complex.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

from ecourts_client._session import Session, get_warm_session
from ecourts_client.errors import CNRNotFound
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind
from ecourts_client.models import (
    Case,
    CaseStub,
    CaseTypeRef,
    CauseList,
    CourtComplexRef,
    DailyBusiness,
    DistrictRef,
    PoliceStationRef,
    StateRef,
)
from ecourts_client.parsers.case_history import parse_case_history
from ecourts_client.parsers.cause_list import parse_cause_list
from ecourts_client.parsers.daily_business import parse_daily_business
from ecourts_client.parsers.dropdowns import (
    parse_court_complexes,
    parse_districts,
    parse_states,
)
from ecourts_client.parsers.dropdowns_extra import (
    parse_case_types,
    parse_police_stations,
)
from ecourts_client.parsers.fir_search import parse_fir_search
from ecourts_client.parsers.search import parse_case_number_search, parse_party_search
from ecourts_client.pdf import fetch_order_pdf


@dataclass
class DistrictCourtClient:
    scope: str = "district"
    # Multi-forum adapter contract (see forums.ForumAdapter). ClassVar so it
    # isn't a dataclass field; fetch_case/fetch_pdf below satisfy the Protocol.
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.ECOURTS_DISTRICT,
        identifier_kind=IdentifierKind.CNR,
        supports_fetch=True,
        supports_search=True,
        supports_pdf=True,
        is_manual=False,
    )
    _session: Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = get_warm_session("district")

    def fetch_case(self, cnr: str) -> Case:
        list_resp = self._session.call(
            "listOfCasesWebService.php",
            {
                "cino": cnr,
                "version_number": "3.0",
                "language_flag": "english",
                "bilingual_flag": "0",
            },
        )
        case_number = list_resp.get("case_number")

        if case_number:
            # eCourts v4.0: caseHistoryWebService keys on ``cino`` (the v3 ``cinum``
            # now returns ``error_ERROR_State_code1``). See docs/RE_NOTES_v4.md.
            history_resp = self._session.call(
                "caseHistoryWebService.php",
                {"cino": cnr, "language_flag": "english", "bilingual_flag": "0"},
            )
        else:
            history_resp = self._session.call(
                "filingCaseHistory.php",
                {"cino": cnr, "language_flag": "english", "bilingual_flag": "0"},
            )

        if not history_resp.get("history"):
            raise CNRNotFound(cnr=cnr)

        return parse_case_history(history_resp, cnr=cnr)

    def fetch_pdf(self, url: str) -> bytes:
        return fetch_order_pdf(self._session, url)

    # --- dropdown listers -------------------------------------------------

    def list_states(self) -> list[StateRef]:
        resp = self._session.call(
            "stateWebService.php",
            {"action_code": "fillState", "time": str(time.time())},
        )
        return parse_states(resp)

    def list_districts(self, state_code: str) -> list[DistrictRef]:
        resp = self._session.call(
            "districtWebService.php",
            {"state_code": str(state_code), "test_param": "pending"},
        )
        return parse_districts(resp, state_code=str(state_code))

    def list_court_complexes(self, *, state_code: str, district_code: str) -> list[CourtComplexRef]:
        resp = self._session.call(
            "courtEstWebService.php",
            {
                "action_code": "fillCourtComplex",
                "state_code": str(state_code),
                "dist_code": str(district_code),
            },
        )
        return parse_court_complexes(resp, state_code=str(state_code), district_code=str(district_code))

    def list_police_stations(
        self, *, state_code: str, district_code: str, court_code: str
    ) -> list[PoliceStationRef]:
        resp = self._session.call(
            "policeStationWebService.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                # v4.0: the DC dropdown listers take the establishment code under
                # ``court_code_arr`` (the array/CSV param, as the search endpoints
                # do). The singular ``court_code`` is rejected with
                # ``error_ERROR_courtcode4`` -- it survives only on the
                # single-court endpoints (cause-list / daily-business). See
                # docs/RE_NOTES_v4.md.
                "court_code_arr": str(court_code),
                "language_flag": "english",
                "bilingual_flag": "0",
            },
        )
        return parse_police_stations(resp, district_code=str(district_code), court_code=str(court_code))

    def list_case_types(
        self, *, state_code: str, district_code: str, court_code: str
    ) -> list[CaseTypeRef]:
        # eCourts v4.0 renamed caseNumberWebService.php -> caseTypesWebService.php
        # AND moved the court selector to ``court_code_arr`` (the array/CSV param).
        # The singular ``court_code`` is rejected with ``error_ERROR_courtcode4``;
        # the v4 app sends ``{state_code, dist_code, court_code_arr, bilingual_flag,
        # language_flag}`` (disasm fetchCaseTypes payload). See docs/RE_NOTES_v4.md.
        resp = self._session.call(
            "caseTypesWebService.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "court_code_arr": str(court_code),
                "language_flag": "english",
                "bilingual_flag": "0",
            },
        )
        return parse_case_types(resp, court_code=str(court_code))

    # --- search modes -----------------------------------------------------

    def search_by_party_name(
        self,
        *,
        state_code: str,
        district_code: str,
        court_code_arr: str,
        party_name: str,
        year: int,
        pending_disposed: str = "Pending",
    ) -> list[CaseStub]:
        """`pending_disposed` must be 'Pending', 'Disposed', or 'Both' -- the same string
        the eCourts UI radio buttons emit. `court_code_arr` is the comma-joined list of
        njdg_est_codes within the selected complex (e.g. '1' for a single complex)."""
        if pending_disposed not in {"Pending", "Disposed", "Both"}:
            raise ValueError(f"pending_disposed must be Pending|Disposed|Both, got {pending_disposed!r}")
        # eCourts v4.0 renamed showDataWebService.php -> searchByPartyName.php.
        resp = self._session.call(
            "searchByPartyName.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "court_code_arr": str(court_code_arr),
                "language_flag": "english",
                "bilingual_flag": "0",
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
        district_code: str,
        court_code_arr: str,
        case_type: str,
        case_number: str,
        year: int,
    ) -> list[CaseStub]:
        resp = self._session.call(
            "caseNumberSearch.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "court_code_arr": str(court_code_arr),
                "language_flag": "english",
                "bilingual_flag": "0",
                "case_number": str(case_number),
                "case_type": str(case_type),
                "year": str(year),
            },
        )
        return parse_case_number_search(resp)

    def search_by_fir(
        self,
        *,
        state_code: str,
        district_code: str,
        court_code_arr: str,
        police_station_code: str,
        fir_number: str,
        year: int,
        uniform_code: int = 0,
        pending_disposed: str = "Pending",
    ) -> list[CaseStub]:
        if pending_disposed not in {"Pending", "Disposed", "Both"}:
            raise ValueError(f"pending_disposed must be Pending|Disposed|Both, got {pending_disposed!r}")
        resp = self._session.call(
            "firNumberSearch.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "court_code_arr": str(court_code_arr),
                "language_flag": "english",
                "bilingual_flag": "0",
                "police_stationcode": str(police_station_code),
                "firNumber": str(fir_number),
                "year": str(year),
                "pendingDisposed": pending_disposed,
                "uniform_code": str(uniform_code),
            },
        )
        return parse_fir_search(resp)

    # --- daily business ---------------------------------------------------

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
        """Fetch the 'View Business' panel for a specific hearing date.

        The eCourts API expects `nextdate1` in YYYYMMDD form and `businessDate` in
        DD-MM-YYYY -- mirroring the inline `viewBusiness(...)` calls in the
        historyOfCaseHearing HTML.
        """
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

    # --- cause list -------------------------------------------------------

    def fetch_cause_list(
        self,
        *,
        state_code: str,
        district_code: str,
        court_code: str,
        court_no: str,
        list_date: date,
        civil_or_criminal: str = "civ_t",
        today: date | None = None,
    ) -> CauseList:
        """Fetch the cause list for a specific (court_code, court_no) on one date.

        `civil_or_criminal` is the JS app's `flag` value: 'civ_t' for civil cases,
        'cri_t' for criminal. `today` defaults to the host clock; pass it explicitly
        if you want deterministic behaviour around the historical-vs-current cutoff.
        """
        if civil_or_criminal not in {"civ_t", "cri_t"}:
            raise ValueError(f"civil_or_criminal must be civ_t|cri_t, got {civil_or_criminal!r}")
        ref_today = today or date.today()
        sel_prev_days = "1" if list_date < ref_today else "0"
        resp = self._session.call(
            "cases_new.php",
            {
                "state_code": str(state_code),
                "dist_code": str(district_code),
                "flag": civil_or_criminal,
                "selprevdays": sel_prev_days,
                "court_no": str(court_no),
                "court_code": str(court_code),
                "causelist_date": list_date.strftime("%d-%m-%Y"),
                "language_flag": "english",
                "bilingual_flag": "0",
            },
        )
        return parse_cause_list(
            resp,
            state_code=str(state_code),
            district_code=str(district_code),
            court_code=str(court_code),
            court_no=str(court_no),
            list_date=list_date,
            flag=civil_or_criminal,
        )
