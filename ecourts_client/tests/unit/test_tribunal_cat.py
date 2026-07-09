"""CAT tribunal-kind adapter: parser + registry.

Fixture mirrors the REAL partyDetail.php result fragment (captured live 2026-07-07)
with SYNTHETIC parties (real litigants are PII — DPDP)."""
from __future__ import annotations

import pytest

from ecourts_client import Forum, TribunalKind, get_adapter, has_automated_adapter
from ecourts_client.errors import CNRNotFound, ECourtsError
from ecourts_client.tribunal.kinds.cat import (
    CATClient,
    _split_identifier,
    parse_detail_html,
    parse_search_html,
)

_HTML = """
<html><body><table>
<tr><td>Diary No.</td><td>Location</td><td>Case Type</td><td>Case No.</td><td>Date of Filing</td><td>Applicant</td><td>Respondent</td><td>Other Details</td></tr>
<tr><td>8733/2022</td><td>Principal Bench Delhi</td><td>O.A.</td><td>O.A./1/2023</td><td>23/12/2022</td><td>EXAMPLE APPLICANT</td><td>UNION OF INDIA</td>
<td><a href="javascript:popsurety_detailreport('MTEw')">MORE DETAIL</a></td></tr>
</table></body></html>
"""

# Mirrors the REAL Misdetailreport123.php CASE STATUS fragment (captured live
# 2026-07-09) with SYNTHETIC parties/dates (real litigants are PII — DPDP).
# 2-cell label->value rows; filing date sits in the header text.
_DETAIL = """
<html><body><div class="container">
<p>CASE STATUS Diary No.- 8733/2022 EXAMPLE PETITIONER Vs UNION OF INDIA Filing Date : 27/12/2022</p>
<table>
<tr><td>Location</td><td>Delhi</td></tr>
<tr><td>Case Number</td><td>O.A./1/2023</td></tr>
<tr><td>Status / Stage</td><td>DISPOSED</td></tr>
<tr><td>Disposal Nature</td><td>ALLOWED</td></tr>
<tr><td>Date of Disposal</td><td>29/01/2025</td></tr>
<tr><td>In the Court no</td><td></td></tr>
<tr><td>Petitioner(s)</td><td>EXAMPLE PETITIONER (M) ,</td></tr>
<tr><td>Respondent(s)</td><td>UNION OF INDIA (M) , EXAMPLE MINISTRY ,</td></tr>
<tr><td>Subject</td><td>PENSION</td></tr>
</table></div></body></html>
"""

# Synthetic PENDING case — exercises the "Next Listing Date" path (the disposed
# layout above is live-verified; the pending label is inferred, see _NEXT_LABELS).
_DETAIL_PENDING = """
<html><body>
<p>CASE STATUS EXAMPLE PETITIONER Vs UNION OF INDIA Filing Date : 05/03/2024</p>
<table>
<tr><td>Location</td><td>Mumbai</td></tr>
<tr><td>Case Number</td><td>O.A./42/2024</td></tr>
<tr><td>Status / Stage</td><td>PENDING FOR ADMISSION</td></tr>
<tr><td>Next Listing Date</td><td>17/11/2026</td></tr>
<tr><td>Petitioner(s)</td><td>EXAMPLE PETITIONER (F) ,</td></tr>
<tr><td>Respondent(s)</td><td>UNION OF INDIA (M) ,</td></tr>
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


def test_parse_detail_core_disposed():
    c = parse_detail_html(_DETAIL, bench_code="100")
    assert c.cnr == "O.A./1/2023"
    assert c.stage == "DISPOSED"
    assert c.next_hearing_date is None  # disposed → no next date
    assert c.court == "Central Administrative Tribunal — Delhi"
    assert c.filing_date.isoformat() == "2022-12-27"
    assert c.title == "EXAMPLE PETITIONER vs UNION OF INDIA"
    assert {p.role for p in c.parties} == {"petitioner", "respondent"}
    pet = next(p for p in c.parties if p.role == "petitioner")
    # gender marker + trailing comma stripped from the messy CAT party cell
    assert pet.name == "EXAMPLE PETITIONER"


def test_parse_detail_pending_next_date():
    c = parse_detail_html(_DETAIL_PENDING, bench_code="210")
    assert "PENDING" in (c.stage or "")
    assert c.next_hearing_date is not None
    assert c.next_hearing_date.isoformat() == "2026-11-17"
    assert c.court == "Central Administrative Tribunal — Mumbai"


def test_parse_detail_empty_raises():
    with pytest.raises(CNRNotFound):
        parse_detail_html("<html><body>no case row here</body></html>", bench_code="100")


def test_fetch_case_two_hop(monkeypatch):
    """fetch_case does search (partyDetail.php → MORE DETAIL b64) then the CASE
    STATUS data page (Misdetailreport123.php), returning a case WITH stage."""
    client = CATClient()
    seen: list[str] = []

    def fake_get(url, params=None, timeout=None, **k):
        seen.append(url)

        class _R:
            status_code = 200
            text = _HTML if "partyDetail.php" in url else _DETAIL
        return _R()

    monkeypatch.setattr(client._http, "get", fake_get)
    c = client.fetch_case("100:1:1:2023")
    assert any("partyDetail.php" in u for u in seen)
    assert any("Misdetailreport123.php" in u for u in seen)
    assert c.cnr == "O.A./1/2023"
    assert c.stage == "DISPOSED"
    assert c.next_hearing_date is None


def test_fetch_case_no_detail_link_raises(monkeypatch):
    client = CATClient()

    class _R:
        status_code = 200
        text = "<html><body>No record found</body></html>"
    monkeypatch.setattr(client._http, "get", lambda *a, **k: _R())
    with pytest.raises(CNRNotFound):
        client.fetch_case("100:1:1:2023")
