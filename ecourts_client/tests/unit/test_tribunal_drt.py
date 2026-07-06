"""DRT/DRAT tribunal-kind adapter: parser + 2-hop orchestration + registry.

Fixture mirrors the REAL Misdetailreport.php shape (captured live 2026-07-06)
with SYNTHETIC parties (real litigants are PII — DPDP)."""
from __future__ import annotations

import pytest

from ecourts_client import Forum, TribunalKind, get_adapter, has_automated_adapter
from ecourts_client.errors import CNRNotFound, ECourtsError
from ecourts_client.tribunal.kinds.drt import DRTClient, _split_identifier, parse_detail_html

_DETAIL = """
<html><body>
<table>
<tr><td>Diary no/Year</td><td>6649/2022</td></tr>
<tr><td>Case Type/Case No/Year</td><td>Original Application/1/2023</td></tr>
<tr><td>Date of Filing.</td><td>27/12/2022</td></tr>
<tr><td>Case Status.</td><td>Pending</td></tr>
<tr><td>Court No.</td><td>1</td></tr>
<tr><td>Next Listing Date</td><td>17/11/2026</td></tr>
<tr><td>Next Listing Purpose</td><td>MISCELLANEOUS</td></tr>
</table>
<div>PETITIONER/APPLICANT DETAIL Petitioner Name   -EXAMPLE BANK OF INDIA Petitioner/Applicant Address: SOME ADDRESS Additional Party:</div>
<div>RESPONDENTS/DEFENDENT DETAILS Respondent Name   -M/S EXAMPLE STEEL ROLLS Respondent/Defendent Address: ANOTHER ADDRESS</div>
<table>
<tr><td>Court Name</td><td>Causelist Date</td><td>Purpose</td></tr>
<tr><td>PO</td><td>16/04/2026</td><td>MISCELLANEOUS</td>
<tr><td>Registrar</td><td>16/02/2026</td><td>Pleading Stage</td>
</table>
</body></html>
"""
_SEARCH = """<html><body><table><tr>
<td>6649/2022</td><td>OA</td><td>OA/1/2023</td><td>---</td><td>27/12/2022</td>
<td><a href="javascript:popsurety_detailreport('MDcwMTEwMDY2NDkyMDIyL2RlbGhp')">MORE DETAIL</a></td>
</tr></table></body></html>"""


def test_parse_detail_core():
    c = parse_detail_html(_DETAIL, sc="delhi")
    assert c.cnr == "Original Application/1/2023"
    assert c.title == "EXAMPLE BANK OF INDIA vs M/S EXAMPLE STEEL ROLLS"
    assert c.court == "Debt Recovery Tribunal (delhi)"
    assert c.stage == "Pending"
    assert c.next_hearing_date.isoformat() == "2026-11-17"
    assert c.filing_date.isoformat() == "2022-12-27"
    assert [h.hearing_date.isoformat() for h in c.history] == ["2026-04-16", "2026-02-16"]
    assert c.history[0].judge == "PO"


def test_drat_court_label():
    c = parse_detail_html(_DETAIL, sc="delhidrat")
    assert c.court == "Debt Recovery Appellate Tribunal (delhidrat)"


def test_empty_detail_is_cnr_not_found():
    with pytest.raises(CNRNotFound):
        parse_detail_html("<html><body>nothing</body></html>", sc="delhi")


def test_split_identifier():
    assert _split_identifier("delhi:1:1:2023") == ("delhi", "1", "1", "2023")
    for bad in ("", "delhi:1:1", "delhi:1:1:2023:x", "delhi::1:2023"):
        with pytest.raises(ECourtsError):
            _split_identifier(bad)


def test_registry_drt_and_drat():
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.DRT) is True
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.DRAT) is True
    assert isinstance(get_adapter(Forum.TRIBUNAL, kind=TribunalKind.DRT), DRTClient)
    assert isinstance(get_adapter(Forum.TRIBUNAL, kind=TribunalKind.DRAT), DRTClient)


def test_fetch_case_two_hop(monkeypatch):
    client = DRTClient()

    def _fake_get(path, params):
        return _SEARCH if path == "partyDetail.php" else _DETAIL
    monkeypatch.setattr(client, "_get", _fake_get)
    c = client.fetch_case("delhi:1:1:2023")
    assert c.cnr == "Original Application/1/2023"
    assert c.court == "Debt Recovery Tribunal (delhi)"


def test_fetch_case_no_detail_link_is_not_found(monkeypatch):
    client = DRTClient()
    monkeypatch.setattr(client, "_get", lambda path, params: "<html>no results</html>")
    with pytest.raises(CNRNotFound):
        client.fetch_case("delhi:1:999:2023")
