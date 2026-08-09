"""TDSAT tribunal-kind adapter: parser + registry.

Fixture mirrors the REAL CASE STATUS page shape (captured live 2026-07-06) with
SYNTHETIC parties/advocates (real litigants are PII — DPDP)."""
from __future__ import annotations

import pytest

from ecourts_client import Forum, TribunalKind, get_adapter, has_automated_adapter
from ecourts_client.errors import CNRNotFound, ECourtsError
from ecourts_client.tribunal.kinds.tdsat import TDSATClient, _split_identifier, parse_status_html

_HTML = """
<html><body>
<table>
<tr><td>Diary no/Year</td><td>8/2023</td></tr>
<tr><td>Case Type/Case No/Year</td><td>Telecom Petition/1/2023</td></tr>
<tr><td>Date of Filing .</td><td>05/01/2023</td></tr>
<tr><td>Case Status.</td><td>Disposed</td></tr>
<tr><td>Date of Disposal</td><td>30/05/2025</td></tr>
<tr><td>Disposal Nature</td><td>ALLOWED AND DISPOSED OF</td></tr>
</table>
<div>PETITIONER DETAIL Petitioner Name   -ACME TELECOM LTD Additional Party(Pet.): Pet. Advocate Name: ADV P Q Additional Advocate(Pet.):</div>
<div>RESPONDENTS DETAIL Respondent Name   -UNION OF INDIA Additional Party(Res.): Respondent Advocate  -ADV R S Additional Advocate(Res.):</div>
<table>
<tr><th>Bench No</th><th>Hearing Date</th><th>Purpose</th><th>Status</th><th>Order</th></tr>
<tr><td>1</td><td>09/01/2023</td><td>For Preliminary Hearing</td><td>P</td><td>Adjourned</td></tr>
<tr><td>1</td><td>24/02/2023</td><td>For Directions</td><td>P</td><td><a href="javascript:popsurety_pet_adv_name('NjAzMzM=')">Order</a></td></tr>
</table>
</body></html>
"""


def test_parse_core_fields():
    c = parse_status_html(_HTML, casetype="2", caseno="1", caseyear="2023")
    assert c.cnr == "Telecom Petition/1/2023"
    assert c.title == "ACME TELECOM LTD vs UNION OF INDIA"
    assert c.court.startswith("Telecom Disputes Settlement")
    assert c.stage.startswith("Disposed") and "ALLOWED" in c.stage
    assert c.filing_date.isoformat() == "2023-01-05"
    assert c.next_hearing_date is None


def test_parse_parties_and_history():
    c = parse_status_html(_HTML, casetype="2", caseno="1", caseyear="2023")
    roles = {p.role: (p.name, p.advocate) for p in c.parties}
    assert roles["petitioner"] == ("ACME TELECOM LTD", "ADV P Q")
    assert roles["respondent"] == ("UNION OF INDIA", "ADV R S")
    assert [h.hearing_date.isoformat() for h in c.history] == ["2023-01-09", "2023-02-24"]
    assert c.history[0].purpose == "For Preliminary Hearing"


# The REAL page (captured 2026-08-09, Broadcasting Petition/345/2024) carries a
# separate "ORDER DETAILS" table BELOW the hearing table, with its own Order Date
# column — and that column uses DD-MM-YYYY, not DD/MM/YYYY. Structure verbatim;
# parties synthetic (real litigants are PII).
_HTML_ORDER_DETAILS = """
<html><body>
<table>
<tr><td>Case Type/Case No/Year</td><td>Broadcasting Petition/345/2024</td></tr>
<tr><td>Case Status.</td><td>Pending</td></tr>
</table>
<div>PETITIONER DETAIL Petitioner Name   -ACME LTD Pet. Advocate Name: ADV P Q</div>
<div>RESPONDENTS DETAIL Respondent Name   -BETA LLP Respondent Advocate  -ADV R S</div>
<table>
<tr><th>Bench No</th><th>Hearing Date</th><th>Purpose</th><th>Status</th><th>Order</th></tr>
<tr><td>1</td><td>06/05/2026</td><td>For Issues</td><td>P</td><td>Adjourned</td></tr>
<tr><td>1</td><td>25/05/2026</td><td>For Issues</td><td>P</td><td>Adjourned</td></tr>
</table>
<tr><th colspan="8">ORDER DETAILS</th></tr>
<tr><th>Serial No.</th><th>Case No.</th><th>Party Detail</th><th>Order Date</th></tr>
<tr>
  <td colspan="1">1</td>
  <td colspan="2"><a href="javascript:popsurety_pet_adv_name('NzI4Mzg=');">BROADCASTING PETITION/345/2024</a></td>
  <td colspan="2">ACME LTD VS BETA LLP</td>
  <td colspan="2"> 13-07-2026</td>
</tr>
<tr>
  <td colspan="1">2</td>
  <td colspan="2"><a href="javascript:popsurety_pet_adv_name('NzIxMTA=');">BROADCASTING PETITION/345/2024</a></td>
  <td colspan="2">ACME LTD VS BETA LLP</td>
  <td colspan="2"> 25-05-2026</td>
</tr>
<tr>
  <td colspan="1">3</td>
  <td colspan="2"><a href="javascript:popsurety_pet_adv_name('NzEzNDk=');">BROADCASTING PETITION/345/2024</a></td>
  <td colspan="2">ACME LTD VS BETA LLP</td>
  <td colspan="2"> 06-05-2026</td>
</tr>
</body></html>
"""


def test_order_dates_come_from_the_order_details_column():
    """Every order used to collapse onto ONE date — the case's latest hearing.

    Two compounding causes, both visible above: the ORDER DETAILS rows put the
    date AFTER the link (the old rule took the nearest PRECEDING date, and every
    link sits below the whole hearing table), and the column is DD-MM-YYYY while
    the date regex only matched DD/MM/YYYY, so the real dates were invisible.
    Live on 2026-08-09 all 13 orders of Broadcasting Petition/345/2024 came back
    as 2026-05-25; 3 of 3 TDSAT cases with multiple orders were affected.
    """
    c = parse_status_html(_HTML_ORDER_DETAILS, casetype="1", caseno="345", caseyear="2024")
    got = {o.order_id: o.order_date.isoformat() for o in c.orders}
    assert got == {
        "NzI4Mzg=": "2026-07-13",
        "NzIxMTA=": "2026-05-25",
        "NzEzNDk=": "2026-05-06",
    }
    # and they are NOT all the latest hearing date
    assert len({o.order_date for o in c.orders}) == 3


def test_empty_page_is_cnr_not_found():
    with pytest.raises(CNRNotFound):
        parse_status_html("<html><body>no case here</body></html>", casetype="2", caseno="9", caseyear="2099")


def test_parse_extracts_order_metadata():
    c = parse_status_html(_HTML, casetype="2", caseno="1", caseyear="2023")
    assert len(c.orders) == 1
    o = c.orders[0]
    assert o.order_id == "NjAzMzM="
    assert o.order_date.isoformat() == "2023-02-24"  # nearest preceding hearing date
    assert o.order_url.endswith("daily_order_view.php?filing_no=NjAzMzM=")
    assert o.order_text is None  # parse is pure; text fetched by the client


def test_inline_order_text(monkeypatch):
    client = TDSATClient()
    client.max_inline_orders = 3

    class _R:
        status_code = 200
        text = "<html><body>ORDER SHEET. Petition allowed. Signed, Registrar.</body></html>"
    monkeypatch.setattr(client._http, "get", lambda *a, **k: _R())
    base = parse_status_html(_HTML, casetype="2", caseno="1", caseyear="2023")
    out = client._inline_order_text(base)
    assert "Petition allowed" in out.orders[0].order_text


def test_inline_order_text_strips_the_portal_php_fatal_error(monkeypatch):
    """orderp.php raises in dompdf AFTER emitting the order, so EVERY TDSAT order
    sheet comes back HTTP 200 with a PHP Fatal error banner glued to the end.

    Banner text below is the real one captured from tdsat.gov.in on 2026-08-09.
    It reached users: casepilot renders order_text into a PDF and 17 documents
    shipped with '…thrown in …/Dompdf.php on line 313' printed at the bottom.
    """
    client = TDSATClient()
    client.max_inline_orders = 3

    class _R:
        status_code = 200
        text = (
            "<html><body>ORDER At the request by the Counsel for the Respondents, "
            "these matters are adjourned to 13.7.2026 for framing of Issues. "
            "( SHASHI KANT SHARMA) DEPUTY REGISTRAR"
            "<b>Fatal error</b> :  Uncaught Error: Call to undefined function "
            "Dompdf\\mb_internal_encoding() in "
            "/home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php:313 Stack trace: "
            "#0 /home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php(287): "
            "Dompdf\\Dompdf-&gt;setPhpConfig() #1 "
            "/home/www/html/tdsat/Delhi/services/orderp.php(17): "
            "Dompdf\\Dompdf-&gt;__construct() #2 {main} thrown in "
            "/home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php on line 313"
            "</body></html>"
        )

    monkeypatch.setattr(client._http, "get", lambda *a, **k: _R())
    base = parse_status_html(_HTML, casetype="2", caseno="1", caseyear="2023")
    text = client._inline_order_text(base).orders[0].order_text

    assert "adjourned to 13.7.2026 for framing of Issues" in text
    assert text.endswith("DEPUTY REGISTRAR")
    for leak in ("Fatal error", "Dompdf", "Stack trace", "thrown in", "on line 313"):
        assert leak not in text, leak


def test_inline_order_text_none_when_the_sheet_is_only_an_error(monkeypatch):
    """A sheet that failed to render carries no order — better to surface nothing
    than an empty shell that reads like an order with no content."""
    client = TDSATClient()
    client.max_inline_orders = 3

    class _R:
        status_code = 200
        text = (
            "<html><body><b>Fatal error</b> :  Uncaught Error: boom in "
            "/home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php:313 "
            "Stack trace: #0 {main} thrown in "
            "/home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php on line 313"
            "</body></html>"
        )

    monkeypatch.setattr(client._http, "get", lambda *a, **k: _R())
    base = parse_status_html(_HTML, casetype="2", caseno="1", caseyear="2023")
    assert client._inline_order_text(base).orders[0].order_text is None


def test_split_identifier():
    assert _split_identifier("2:1:2023") == ("2", "1", "2023")
    for bad in ("", "2:1", "2:1:2023:x", "2::2023"):
        with pytest.raises(ECourtsError):
            _split_identifier(bad)


def test_registry():
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.TDSAT) is True
    a = get_adapter(Forum.TRIBUNAL, kind=TribunalKind.TDSAT)
    assert isinstance(a, TDSATClient)
    assert a.capabilities.tribunal_kind is TribunalKind.TDSAT


def test_fetch_case_orchestration(monkeypatch):
    client = TDSATClient()

    class _Resp:
        status_code = 200
        text = _HTML
    monkeypatch.setattr(client._http, "post", lambda *a, **k: _Resp())
    c = client.fetch_case("2:1:2023")
    assert c.cnr == "Telecom Petition/1/2023"
