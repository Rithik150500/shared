"""CAT tribunal-kind adapter: parser + registry.

Fixture mirrors the REAL partyDetail.php result fragment (captured live 2026-07-07)
with SYNTHETIC parties (real litigants are PII — DPDP)."""
from __future__ import annotations

import pytest

from ecourts_client import Forum, TribunalKind, get_adapter, has_automated_adapter
from ecourts_client.errors import CNRNotFound, ECourtsError
from ecourts_client.tribunal.kinds.cat import CATClient, _split_identifier, parse_search_html

_HTML = """
<html><body><table>
<tr><td>Diary No.</td><td>Location</td><td>Case Type</td><td>Case No.</td><td>Date of Filing</td><td>Applicant</td><td>Respondent</td><td>Other Details</td></tr>
<tr><td>8733/2022</td><td>Principal Bench Delhi</td><td>O.A.</td><td>O.A./1/2023</td><td>23/12/2022</td><td>EXAMPLE APPLICANT</td><td>UNION OF INDIA</td>
<td><a href="javascript:popsurety_detailreport('MTEw')">MORE DETAIL</a></td></tr>
</table></body></html>
"""


def test_parse_core_fields():
    c = parse_search_html(_HTML, bench_code="100")
    assert c.cnr == "O.A./1/2023"
    assert c.title == "EXAMPLE APPLICANT vs UNION OF INDIA"
    assert c.court == "Central Administrative Tribunal — Principal Bench Delhi"
    assert c.filing_date.isoformat() == "2022-12-23"
    assert {p.role for p in c.parties} == {"petitioner", "respondent"}
    assert c.next_hearing_date is None and c.stage is None  # detail deferred


def test_empty_result_is_cnr_not_found():
    with pytest.raises(CNRNotFound):
        parse_search_html("<html><body>No record found</body></html>", bench_code="100")


def test_split_identifier():
    assert _split_identifier("100:1:1:2023") == ("100", "1", "1", "2023")
    for bad in ("", "100:1:1", "100:1:1:2023:x", "100::1:2023"):
        with pytest.raises(ECourtsError):
            _split_identifier(bad)


def test_registry():
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.CAT) is True
    a = get_adapter(Forum.TRIBUNAL, kind=TribunalKind.CAT)
    assert isinstance(a, CATClient)
    assert a.capabilities.tribunal_kind is TribunalKind.CAT


def test_fetch_case_orchestration(monkeypatch):
    client = CATClient()

    class _R:
        status_code = 200
        text = _HTML
    monkeypatch.setattr(client._http, "get", lambda *a, **k: _R())
    c = client.fetch_case("100:1:1:2023")
    assert c.cnr == "O.A./1/2023"
    assert c.court.endswith("Principal Bench Delhi")
