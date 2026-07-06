"""NCLAT tribunal-kind adapter: parser + client orchestration + registry.

Fixture mirrors the REAL view_details JSON shape (captured live 2026-07-06) but
with SYNTHETIC parties/advocates (real litigants are PII — DPDP)."""
from __future__ import annotations

import pytest

from ecourts_client import Forum, TribunalKind, get_adapter, has_automated_adapter
from ecourts_client.errors import CNRNotFound, ECourtsError
from ecourts_client.tribunal.kinds.nclat import (
    BASE_URL,
    NCLATClient,
    _split_identifier,
    parse_view_details,
)

_CORAM = " Justice A B (Chairperson) , Hon'ble Mr. C D (Member (Technical)) , "
_DATA = {
    "case_details": [{
        "filing_no": "9910110081912022", "status": "D", "case_no": "1",
        "case_year": "2023", "case_type": "Company Appeal(AT)(Ins)",
        "date_of_filing": "2022-12-14", "registration_date": "2023-01-02",
    }],
    "party_details": {
        "applicant_name": [{"name": "ACME DEVELOPERS PRIVATE LIMITED"}],
        "respondant_name": [{"name": "Mr. A B Resolution Professional"}, {"name": "Some Bank Ltd"}],
    },
    "legal_representative": {
        "applicant_legal_representative_name": [{"name": "Adv. P Q"}],
        "respondent_legal_representative_name": "Adv. R S",
    },
    "first_hearing_details": {"court_no": 1, "hearing_date": "2023-01-04", "stage_of_case": "For Admission (Fresh Case)", "coram": _CORAM},
    "last_hearing_details": {"court_no": 1, "hearing_date": "2023-01-13", "stage_of_case": "For Admission (Fresh Case)", "coram": _CORAM},
    "next_hearing_details": {"court_no": 1, "hearing_date": "2023-01-13", "stage_of_case": "For Admission (Fresh Case)", "coram": _CORAM},
    "case_history": [
        {"hearing_date": "2023-01-04", "purpose": "For Admission (Fresh Case)", "court_no": 1},
        {"hearing_date": "2023-01-13", "purpose": "For Orders", "court_no": 1},
    ],
    "order_history": [
        {"order_date": "2023-01-13", "order_type": "J", "order_pdf_download": "/NCLAT_Documents/CIS_Documents/casedoc/orders/DELHI/2023-01-13/courts/1/daily/abc123.pdf"},
    ],
}


def test_parse_core_fields():
    c = parse_view_details(_DATA, location="delhi")
    assert c.cnr == "Company Appeal(AT)(Ins) 1/2023"
    assert c.title == "ACME DEVELOPERS PRIVATE LIMITED vs Mr. A B Resolution Professional & Ors."
    assert c.court == "National Company Law Appellate Tribunal, New Delhi"
    # status="D" (Disposed): show the disposal status, not the stale last-hearing
    # stage NCLAT keeps echoing in next_hearing_details; and no upcoming hearing.
    assert c.stage == "Disposed"
    assert c.next_hearing_date is None
    assert c.filing_date.isoformat() == "2022-12-14"
    assert "Justice A B" in c.judge and c.judge.endswith("Technical))")  # trailing comma/space stripped


def test_pending_case_shows_current_stage():
    import copy
    data = copy.deepcopy(_DATA)
    data["case_details"][0]["status"] = "P"
    c = parse_view_details(data, location="delhi")
    # Pending: the current stage_of_case is most informative; next hearing kept.
    assert c.stage == "For Admission (Fresh Case)"
    assert c.next_hearing_date.isoformat() == "2023-01-13"


def test_parse_parties_history_orders():
    c = parse_view_details(_DATA, location="delhi")
    pet = [p for p in c.parties if p.role == "petitioner"]
    res = [p for p in c.parties if p.role == "respondent"]
    assert [p.name for p in pet] == ["ACME DEVELOPERS PRIVATE LIMITED"]
    assert pet[0].advocate == "Adv. P Q"
    assert [p.name for p in res] == ["Mr. A B Resolution Professional", "Some Bank Ltd"]
    assert res[0].advocate == "Adv. R S"
    assert len(c.history) == 2 and c.history[1].purpose == "For Orders"
    # parse_view_details yields order METADATA only (order_date + stable id); the
    # PDF is inlined by the client's _inline_orders (I/O). The direct .pdf URL 404s
    # so order_url is left empty (download goes via POST view_order).
    assert len(c.orders) == 1
    assert c.orders[0].order_date.isoformat() == "2023-01-13"
    assert c.orders[0].order_id == "abc123.pdf"
    assert c.orders[0].order_url == ""
    assert c.orders[0].inline_pdf_b64 is None


def test_inline_orders_downloads_latest_n(monkeypatch):
    import base64
    from ecourts_client.tribunal.kinds.nclat import NCLATClient
    client = NCLATClient()
    client.max_inline_orders = 1
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append((url, dict(data or {})))

        class _R:
            status_code = 200
            content = b"%PDF-1.4 fake order"
        return _R()

    monkeypatch.setattr(client._http, "post", fake_post)
    base = parse_view_details(_DATA, location="delhi")
    out = client._inline_orders(
        base, _DATA["order_history"], token="tok", filing_no="9910110081912022", bench="delhi"
    )
    o = out.orders[0]
    assert base64.b64decode(o.inline_pdf_b64).startswith(b"%PDF")
    # posted to view_order (urlencoded) with all SIX fields incl. order_type + search_type
    assert calls and calls[0][0].endswith("/display-board/view_order")
    assert calls[0][1] == {
        "search_type": "view_order", "_token": "tok", "bench_name": "delhi",
        "filing_no": "9910110081912022", "order_date": "2023-01-13", "order_type": "J",
    }


def test_inline_orders_skips_non_pdf(monkeypatch):
    from ecourts_client.tribunal.kinds.nclat import NCLATClient
    client = NCLATClient()
    client.max_inline_orders = 1

    class _R:
        status_code = 200
        content = b"<html>No document</html>"
    monkeypatch.setattr(client._http, "post", lambda *a, **k: _R())
    out = client._inline_orders(
        parse_view_details(_DATA, location="delhi"), _DATA["order_history"],
        token="t", filing_no="f", bench="delhi",
    )
    assert out.orders[0].inline_pdf_b64 is None  # non-PDF response → not inlined


def test_empty_detail_is_cnr_not_found():
    with pytest.raises(CNRNotFound):
        parse_view_details({"case_details": []}, location="delhi")


def test_split_identifier():
    assert _split_identifier("delhi:33:1:2023") == ("delhi", "33", "1", "2023")
    for bad in ("", "delhi:33:1", "delhi:33:1:2023:x", "mars:33:1:2023", "delhi::1:2023"):
        with pytest.raises(ECourtsError):
            _split_identifier(bad)


def test_registry_per_kind():
    # NCLAT kind is registered + automated…
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.NCLAT) is True
    a = get_adapter(Forum.TRIBUNAL, kind=TribunalKind.NCLAT)
    assert isinstance(a, NCLATClient)
    assert a.capabilities.tribunal_kind is TribunalKind.NCLAT
    assert a.capabilities.supports_fetch is True
    # …but the bare tribunal forum + other kinds are NOT (still manual).
    assert has_automated_adapter(Forum.TRIBUNAL) is False
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.CAT) is False


def test_fetch_case_orchestration(monkeypatch):
    client = NCLATClient()
    monkeypatch.setattr(client, "_open_session", lambda: "tok")
    monkeypatch.setattr(client, "_search_filing_no", lambda *a: "9910110081912022")
    monkeypatch.setattr(client, "_post", lambda path, payload: {"data": _DATA})
    c = client.fetch_case("delhi:33:1:2023")
    assert c.cnr == "Company Appeal(AT)(Ins) 1/2023"
    assert c.court.endswith("New Delhi")


def test_fetch_case_not_found_propagates(monkeypatch):
    client = NCLATClient()
    monkeypatch.setattr(client, "_open_session", lambda: "tok")

    def _empty(*a):
        raise CNRNotFound(cnr="nclat:delhi:33:999:2023")
    monkeypatch.setattr(client, "_search_filing_no", _empty)
    with pytest.raises(CNRNotFound):
        client.fetch_case("delhi:33:999:2023")
