"""Supreme Court adapter: parser + client + registry.

Fixture mirrors the REAL pageid=030001 case-detail HTML shape (label→value rows,
captured live 2026-07-05) but with SYNTHETIC parties (real litigants are PII)."""
from __future__ import annotations

import os

import pytest

from ecourts_client import Forum, get_adapter, has_automated_adapter
from ecourts_client.errors import CNRNotFound, IdentifierMalformed
from ecourts_client.supreme.client import SupremeCourtClient, _split_identifier
from ecourts_client.supreme.parsers import parse_case_html
from ecourts_client.supreme._session import SCTokenMissing, SupremeSession

_HTML = """
<html><body><table><tbody>
<tr><td>Diary No.</td><td>5/2026 Filed on 01-01-2026 12:45 AM [SECTION: II-B ]</td></tr>
<tr><td>Case No.</td><td>SLP(Crl) No. 003159 -  / 2026&nbsp;&nbsp;Registered on 18-02-2026 (Verified On 17-01-2026)</td></tr>
<tr><td>Present/Last Listed On</td><td>17-03-2026 [ HON'BLE MR. JUSTICE A B and HON'BLE MR. JUSTICE C D ]</td></tr>
<tr><td>Status/Stage</td><td>DISPOSED (Motion Hearing [BAIL MATTERS]) Dismissed-Ord dt:17-03-2026 (Disposal Date: 17-03-2026)</td></tr>
<tr><td>Disp.Type</td><td>Dismissed</td></tr>
<tr><td>Petitioner(s)</td><td>1 TEST PETITIONER</td></tr>
<tr><td>Respondent(s)</td><td>1 TEST STATE</td></tr>
<tr><td>Pet. Advocate(s)</td><td>ADV ONE</td></tr>
<tr><td>Resp. Advocate(s)</td><td>ADV TWO</td></tr>
</tbody></table></body></html>
"""


def test_parse_case_core_fields():
    c = parse_case_html(_HTML, diary_no="5", diary_yr="2026")
    assert c.cnr == "5/2026"
    assert c.title == "TEST PETITIONER vs TEST STATE"
    assert c.court == "Supreme Court of India"
    assert c.stage.startswith("DISPOSED")
    # DISPOSED matter: "Present/Last Listed On" is the past disposal listing, not
    # a future hearing — it must NOT surface as next_hearing_date.
    assert c.next_hearing_date is None
    assert c.filing_date.isoformat() == "2026-01-01"
    assert "JUSTICE A B" in c.judge and "JUSTICE C D" in c.judge


_HTML_PENDING = """
<html><body><table><tbody>
<tr><td>Diary No.</td><td>7/2026 Filed on 01-01-2026 12:45 AM</td></tr>
<tr><td>Case No.</td><td>SLP(Crl) No. 003160 - / 2026</td></tr>
<tr><td>Present/Last Listed On</td><td>17-03-2026 [ HON'BLE MR. JUSTICE A B ]</td></tr>
<tr><td>Status/Stage</td><td>PENDING (Motion Hearing [BAIL MATTERS])</td></tr>
<tr><td>Petitioner(s)</td><td>1 TEST PETITIONER</td></tr>
<tr><td>Respondent(s)</td><td>1 TEST STATE</td></tr>
</tbody></table></body></html>
"""


def test_pending_case_keeps_next_hearing_date():
    # A PENDING SC matter must keep its listed date (no false-positive suppression).
    c = parse_case_html(_HTML_PENDING, diary_no="7", diary_yr="2026")
    assert c.stage.startswith("PENDING")
    assert c.next_hearing_date.isoformat() == "2026-03-17"


def test_parse_parties_and_advocates():
    c = parse_case_html(_HTML, diary_no="5", diary_yr="2026")
    roles = {p.role: (p.name, p.advocate) for p in c.parties}
    assert roles["petitioner"] == ("TEST PETITIONER", "ADV ONE")
    assert roles["respondent"] == ("TEST STATE", "ADV TWO")


def test_empty_page_is_cnr_not_found():
    with pytest.raises(CNRNotFound):
        parse_case_html("<html><body>no case</body></html>", diary_no="9", diary_yr="2099")


def test_split_identifier():
    assert _split_identifier("5:2026") == ("5", "2026")
    for bad in ("", "5", "5:", ":2026", "5:abcd"):
        with pytest.raises(IdentifierMalformed):
            _split_identifier(bad)


def test_registry_registered_and_automated():
    assert has_automated_adapter(Forum.SUPREME_COURT) is True
    assert isinstance(get_adapter(Forum.SUPREME_COURT), SupremeCourtClient)
    assert get_adapter(Forum.SUPREME_COURT).capabilities.supports_fetch is True


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("SC_MOBILE_TOKEN", raising=False)
    s = SupremeSession(token=None)
    with pytest.raises(SCTokenMissing):
        s.get("030001", {"d_no": "5", "d_yr": "2026"})
