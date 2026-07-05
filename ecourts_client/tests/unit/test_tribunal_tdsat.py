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
<tr><td>1</td><td>24/02/2023</td><td>For Directions</td><td>P</td><td>Adjourned</td></tr>
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


def test_empty_page_is_cnr_not_found():
    with pytest.raises(CNRNotFound):
        parse_status_html("<html><body>no case here</body></html>", casetype="2", caseno="9", caseyear="2099")


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
