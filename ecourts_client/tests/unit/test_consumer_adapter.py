"""Consumer forum (e-Jagriti) adapter — unit tests (no network).

Payload shapes mirror docs/spike-ejagriti-transport.md (live-confirmed 2026-07-04).
"""
from __future__ import annotations

from datetime import date

import pytest

from ecourts_client import Forum, get_adapter, has_automated_adapter
from ecourts_client.consumer import CommissionRef, ConsumerClient
from ecourts_client.consumer import client as consumer_client
from ecourts_client.consumer._session import ConsumerSession
from ecourts_client.consumer.parsers import (
    parse_case,
    parse_case_stubs,
    parse_commissions,
)
from ecourts_client.errors import (
    CNRNotFound,
    CourtSiteDown,
    ECourtsError,
    IdentifierMalformed,
)
from ecourts_client.forums import ForumAdapter
from ecourts_client.models import Case


_STATE_DATA = [
    {"commissionId": 11290000, "commissionNameEn": "KARNATAKA", "circuitAdditionBenchStatus": False, "activeStatus": True},
    {"commissionId": 11350000, "commissionNameEn": "ANDAMAN NICOBAR", "circuitAdditionBenchStatus": True, "activeStatus": True},
]
_DISTRICT_DATA = [
    {"commissionId": 11290525, "commissionNameEn": "Bangalore Urban", "circuitAdditionBenchStatus": False, "activeStatus": True},
]
_CASE_ROW = {
    "caseNumber": "SC/29/A/1006/2024",
    "complainantName": "Sharma",
    "complainantAdvocateName": "Adv A",
    "respondentName": "ACME Ltd",
    "respondentAdvocateName": "Adv B",
    "caseFilingDate": "2024-03-15",
    "caseStageName": "DISPOSED OFF",
    "dateOfHearing": "2024-08-12",
    "orderDate": "2024-09-01",
    "orderDocumentPath": "orders/abc.pdf",
    "filingReferenceNumber": 100003601304,
    "additionalRespondantList": [{"name": "Beta Co"}],  # [sic] NIC misspelling
}


class _FakeSession:
    def __init__(self, *, states=None, districts=None, rows=None):
        self._states = _STATE_DATA if states is None else states
        self._districts = _DISTRICT_DATA if districts is None else districts
        self._rows = [_CASE_ROW] if rows is None else rows
        self.calls: list = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if "getStateCommission" in path:
            return self._states
        if "getDistrictCommission" in path:
            return self._districts
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body=None, params=None):
        self.calls.append(("POST", path, body))
        return self._rows

    def fetch_pdf(self, url):
        return b"%PDF-1.7 fake"


def _client(**kw) -> ConsumerClient:
    c = ConsumerClient()
    c._session = _FakeSession(**kw)
    return c


# ---- parsers ----------------------------------------------------------------

def test_parse_commissions():
    refs = parse_commissions(_STATE_DATA)
    assert refs[0] == CommissionRef(11290000, "KARNATAKA", is_bench=False, active=True)
    assert refs[1].is_bench is True


def test_parse_case_stubs_and_case():
    stubs = parse_case_stubs([_CASE_ROW], court="Bangalore Urban")
    assert stubs[0].cnr == "SC/29/A/1006/2024"
    assert stubs[0].case_number == "SC/29/A/1006/2024"
    assert stubs[0].court == "Bangalore Urban"

    c = parse_case(_CASE_ROW, court="Bangalore Urban")
    assert c.cnr == "SC/29/A/1006/2024"
    assert c.title == "Sharma vs ACME Ltd"
    assert c.stage == "DISPOSED OFF"
    assert c.court == "Bangalore Urban"
    assert c.filing_date == date(2024, 3, 15)
    # DISPOSED OFF: the echoed dateOfHearing is not a future hearing.
    assert c.next_hearing_date is None
    roles = {(p.name, p.role) for p in c.parties}
    assert ("Sharma", "complainant") in roles
    assert ("ACME Ltd", "respondent") in roles
    assert ("Beta Co", "respondent") in roles  # additionalRespondantList[sic]
    assert c.orders and c.orders[0].order_date == date(2024, 9, 1)
    assert c.orders[0].order_url == "orders/abc.pdf"


def test_parse_case_tolerates_missing_optional_fields():
    c = parse_case({"caseNumber": "SC/1/2025", "complainantName": "X"})
    assert c.cnr == "SC/1/2025"
    assert c.title == "X"
    assert c.next_hearing_date is None
    assert c.filing_date is None
    assert c.orders == []


def test_pending_case_keeps_next_hearing_date():
    # A PENDING consumer case with a real future listing must keep its date
    # (no false-positive suppression, no dateOfDisposal present).
    c = parse_case({
        "caseNumber": "SC/2/2025", "complainantName": "X",
        "caseStageName": "PENDING", "dateOfHearing": "2099-09-09",
    })
    assert c.stage == "PENDING"
    assert c.next_hearing_date == date(2099, 9, 9)


# ---- identifier + window helpers -------------------------------------------

def test_split_identifier_ok():
    assert consumer_client._split_identifier("11290525:SC/29/A/1006/2024") == (
        11290525, "SC/29/A/1006/2024",
    )


@pytest.mark.parametrize("bad", ["", "nocolon", "abc:CASE", "11290525:"])
def test_split_identifier_bad(bad):
    with pytest.raises(IdentifierMalformed):
        consumer_client._split_identifier(bad)


def test_window_uses_year_suffix():
    frm, to = consumer_client._window_for_case_number("SC/29/A/1006/2020", today=date(2026, 7, 4))
    assert (frm, to) == (date(2020, 1, 1), date(2020, 12, 31))


def test_window_current_year_caps_at_today():
    frm, to = consumer_client._window_for_case_number("SC/1/2026", today=date(2026, 7, 4))
    assert (frm, to) == (date(2026, 1, 1), date(2026, 7, 4))


def test_window_fallback_when_no_year():
    frm, to = consumer_client._window_for_case_number("WEIRD", today=date(2026, 7, 4))
    assert (frm, to) == (date(2023, 1, 1), date(2026, 7, 4))


# ---- client -----------------------------------------------------------------

def test_list_state_and_district():
    c = _client()
    # NCDRC is injected client-side (upstream omits it) and leads the cascade.
    assert [r.name for r in c.list_state_commissions()] == [
        "NCDRC (National Commission)", "KARNATAKA", "ANDAMAN NICOBAR",
    ]
    assert c.list_district_commissions(11290000)[0].commission_id == 11290525


# ---- NCDRC ------------------------------------------------------------------

def test_ncdrc_is_prepended_and_well_formed():
    c = _client()
    ncdrc = c.list_state_commissions()[0]
    assert ncdrc.commission_id == consumer_client.NCDRC_COMMISSION_ID == 11000000
    assert ncdrc.active is True and ncdrc.is_bench is False


def test_ncdrc_survives_an_empty_upstream_lister():
    """A lister outage must not take NCDRC down with it — it isn't sourced there."""
    c = _client(states=[])
    assert [r.commission_id for r in c.list_state_commissions()] == [11000000]


def test_ncdrc_districts_short_circuit_without_a_round_trip():
    c = _client()
    assert c.list_district_commissions(11000000) == []
    assert c.list_district_commissions("11000000") == []
    # The apex commission has no districts; we must not even ask upstream (that
    # endpoint answers [] for ANY id, so a call would be pure latency).
    assert c._session.calls == []


def test_non_ncdrc_districts_still_hit_upstream():
    c = _client()
    assert c.list_district_commissions(11290000)[0].commission_id == 11290525
    assert [k for k, *_ in c._session.calls] == ["GET"]


# ---- name search ------------------------------------------------------------

def test_search_by_name_sends_role_serchtype_and_wide_window():
    c = _client()
    stubs = c.search_by_name(
        commission_id=11000000, name="ANANT RAM", role="complainant",
    )
    assert stubs[0].cnr == "SC/29/A/1006/2024"
    _, _, body = c._session.calls[-1]
    assert body["serchType"] == 2                    # [sic] complainant
    assert body["serchTypeValue"] == "ANANT RAM"     # [sic]
    assert body["commissionId"] == 11000000
    # Window must be wide by default: a narrow one silently returns nothing.
    span = date.fromisoformat(body["toDate"]).year - date.fromisoformat(body["fromDate"]).year
    assert span >= 10


@pytest.mark.parametrize(
    "role,expected", [("complainant", 2), ("respondent", 3),
                      ("complainant_advocate", 4), ("respondent_advocate", 5)],
)
def test_search_by_name_role_maps_to_serchtype(role, expected):
    c = _client()
    c.search_by_name(commission_id=11000000, name="X", role=role)
    assert c._session.calls[-1][2]["serchType"] == expected


def test_search_by_name_rejects_unknown_role_and_blank_name():
    c = _client()
    with pytest.raises(ValueError):
        c.search_by_name(commission_id=11000000, name="X", role="judge")
    with pytest.raises(ValueError):
        c.search_by_name(commission_id=11000000, name="   ")
    assert c._session.calls == []  # rejected before any upstream call


def test_search_by_name_honours_an_explicit_window():
    c = _client()
    c.search_by_name(
        commission_id=11000000, name="X",
        from_date=date(2016, 1, 1), to_date=date(2017, 12, 31),
    )
    body = c._session.calls[-1][2]
    assert body["fromDate"] == "2016-01-01" and body["toDate"] == "2017-12-31"


def test_search_sends_verbatim_misspelled_body():
    c = _client()
    stubs = c.search_by_case_number(
        commission_id=11290525, case_number="1006",
        from_date=date(2024, 1, 1), to_date=date(2024, 12, 31),
    )
    assert stubs[0].cnr == "SC/29/A/1006/2024"
    _, _, body = c._session.calls[-1]
    assert body["serchType"] == 1              # [sic] misspelled key sent verbatim
    assert body["serchTypeValue"] == "1006"    # [sic]
    assert body["commissionId"] == 11290525
    assert body["dateRequestType"] == 1
    assert body["fromDate"] == "2024-01-01"


def test_fetch_case_happy():
    c = _client()
    case = c.fetch_case("11290525:SC/29/A/1006/2024")
    assert isinstance(case, Case)
    assert case.cnr == "SC/29/A/1006/2024"
    assert case.stage == "DISPOSED OFF"


def test_fetch_case_not_found_raises():
    c = _client(rows=[])
    with pytest.raises(CNRNotFound):
        c.fetch_case("11290525:SC/29/A/9999/2024")


# ---- capabilities + registry ------------------------------------------------

def test_capabilities_and_protocol():
    c = ConsumerClient()
    assert c.capabilities.forum is Forum.CONSUMER
    assert c.capabilities.supports_fetch and c.capabilities.supports_search
    assert not c.capabilities.is_manual
    assert isinstance(c, ForumAdapter)


def test_registered_as_consumer_adapter():
    assert has_automated_adapter(Forum.CONSUMER)
    assert isinstance(get_adapter(Forum.CONSUMER), ConsumerClient)


# ---- session envelope / error classification (fake _http, no network) -------

class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeHttp:
    def __init__(self, resp):
        self._resp = resp
        self.headers = {}

    def request(self, *a, **k):
        return self._resp

    def get(self, *a, **k):
        return self._resp


def _session_with(resp) -> ConsumerSession:
    s = ConsumerSession()
    s._http = _FakeHttp(resp)
    return s


def test_session_unwraps_data():
    s = _session_with(_FakeResp(200, {"data": [1, 2], "error": "false", "status": 200}))
    assert s.get("x") == [1, 2]


def test_session_error_true_raises():
    s = _session_with(_FakeResp(200, {"data": None, "error": "true", "message": "bad", "status": 200}))
    with pytest.raises(ECourtsError):
        s.get("x")


def test_session_5xx_is_court_site_down():
    s = _session_with(_FakeResp(503, None, text="oops"))
    with pytest.raises(CourtSiteDown):
        s.get("x")


def test_session_401_raises_hard():
    s = _session_with(_FakeResp(401, {"error": "true", "message": "Access Denied", "status": 401}))
    with pytest.raises(ECourtsError):
        s.get("x")


# ---- review fixes: blocker regressions + robustness ------------------------

def test_fetch_case_no_exact_match_raises_not_found():
    # Rows returned but NONE match the requested number → must NOT substitute a
    # neighbour (the old rows[0] fallback); must raise CNRNotFound. (Blocker.)
    other = {**_CASE_ROW, "caseNumber": "SC/29/A/2222/2024", "complainantName": "Someone Else"}
    c = _client(rows=[other])
    with pytest.raises(CNRNotFound):
        c.fetch_case("11290525:SC/29/A/1006/2024")


class _PagingSession:
    """post() returns a FULL page of non-matching filler until `match_page`."""

    def __init__(self, match_row, *, match_page, size=50):
        self._match = match_row
        self._match_page = match_page
        self._size = size
        self.pages_fetched: list[int] = []

    def post(self, path, body=None, params=None):
        page = body["page"]
        self.pages_fetched.append(page)
        if page == self._match_page:
            return [self._match]
        return [{"caseNumber": f"SC/X/{i}/2024"} for i in range(self._size)]


def test_fetch_case_paginates_to_find_exact():
    c = ConsumerClient()
    c._session = _PagingSession(_CASE_ROW, match_page=2, size=50)
    case = c.fetch_case("11290525:SC/29/A/1006/2024")
    assert case.cnr == "SC/29/A/1006/2024"
    assert c._session.pages_fetched == [0, 1, 2]  # paged forward until the exact match


def test_parse_case_captures_inline_base64_order():
    row = {
        "caseNumber": "SC/1/2024",
        "orderDate": "2024-09-01",
        "documentBase64": "JVBERi0xLjcgZmFrZQ==",  # base64("%PDF-1.7 fake")
    }
    c = parse_case(row)
    assert len(c.orders) == 1
    assert c.orders[0].inline_pdf_b64 == "JVBERi0xLjcgZmFrZQ=="
    assert c.orders[0].order_url == ""  # base64-only: no path


def test_parse_case_tolerates_nonstring_fields():
    # A non-string truthy value must not crash the parser (schema-tolerance).
    c = parse_case({"caseNumber": 12345, "complainantName": {"weird": 1}})
    assert isinstance(c.cnr, str) and c.cnr == "12345"


def test_window_allows_next_fy_near_boundary():
    # Late-year filing carrying next FY: no crash, window ends today (from<=to).
    frm, to = consumer_client._window_for_case_number("SC/1/2027", today=date(2026, 12, 20))
    assert frm <= to and to == date(2026, 12, 20)


def test_session_returns_bare_array_body():
    s = _session_with(_FakeResp(200, [1, 2, 3]))
    assert s.get("x") == [1, 2, 3]


def test_session_nondict_nonlist_body_raises_schema_changed():
    from ecourts_client.errors import SchemaChanged
    s = _session_with(_FakeResp(200, 42))
    with pytest.raises(SchemaChanged):
        s.get("x")


def test_session_bare_403_is_court_site_down():
    s = _session_with(_FakeResp(403, None, text="Forbidden"))
    with pytest.raises(CourtSiteDown):
        s.get("x")


def test_session_geo_403_is_geoip_block():
    from ecourts_client.errors import BlockedByGeoIP
    s = _session_with(_FakeResp(403, None, text="blocked by geo policy"))
    with pytest.raises(BlockedByGeoIP):
        s.get("x")


def test_session_non_json_200_is_court_site_down():
    s = _session_with(_FakeResp(200, None, text="<html>maintenance</html>"))
    with pytest.raises(CourtSiteDown):
        s.get("x")


def test_session_envelope_status_429_is_rate_limited():
    from ecourts_client.errors import RateLimited
    s = _session_with(_FakeResp(200, {"data": None, "error": "true", "status": 429}))
    with pytest.raises(RateLimited):
        s.get("x")


def test_session_envelope_status_5xx_is_court_site_down():
    s = _session_with(_FakeResp(200, {"data": None, "error": "true", "status": 503}))
    with pytest.raises(CourtSiteDown):
        s.get("x")
