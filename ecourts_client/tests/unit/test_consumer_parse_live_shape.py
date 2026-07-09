"""Parser tests pinned to the REAL e-Jagriti getCaseDetailsBySearchType row
shape (captured live from Bangalore Urban DCDRC on 2026-07-04).

Values are anonymized (real rows carry litigants' PII — DPDP), but every KEY
name + value FORMAT mirrors the live payload, including:
  - case number with slashes ("DC/525/CC/101/2023") + ISO dates
  - additionalComplainantList entries keyed on `additional_respondent_name`
  - documentBase64 / judgmentOrderDocumentBase64 being POLYMORPHIC: a real
    base64-encoded PDF on some rows, a raw HTML "daily order" interstitial on
    others (which must NOT be stored as a PDF)
  - judgments arriving on judgemtmentDate/judgmentOrderDocumentBase64[sic], with
    orderDate null (so they'd be lost if only orderDate were consulted)
"""
from __future__ import annotations

import base64

from ecourts_client.consumer.parsers import parse_case, parse_case_stubs

# A minimal but real PDF (header magic is all the validator checks).
_REAL_PDF_B64 = base64.b64encode(
    b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
).decode()

# The HTML "daily order" interstitial served in documentBase64 on ~half the
# rows — a raw <html> string, not base64 (abridged from the live capture).
_HTML_INTERSTITIAL = (
    "<html>\t<head>\t<title>Daily Order</title></head><body>"
    "<b>DIST CONSUMER COMMISSION, BANGALORE URBAN</b>"
    "<p>partly allowed</p></body></html>"
)


def _order_row_with_pdf() -> dict:
    """An order row whose documentBase64 IS a real base64 PDF."""
    return {
        "caseNumber": "DC/525/CC/999/2024",
        "complainantName": "A. Complainant",
        "complainantAdvocateName": "Adv. One",
        "respondentName": "M/s. Respondent Pvt Ltd",
        "respondentAdvocateName": None,
        "caseFilingDate": "2024-03-13",
        "orderDocumentPath": None,
        "orderDate": "2024-11-29",
        "dateOfDisposal": "2024-11-29",
        "caseStageName": "DISPOSED OFF",
        "documentBase64": _REAL_PDF_B64,
        "additionalComplainantList": [{"additional_respondent_name": "B. Co-Complainant"}],
        "additionalRespondantList": None,
        "judgemtmentDate": None,
        "judgmentOrderDocumentBase64": None,
        "dateOfHearing": "2024-11-29",
        "orderAvailabilityStatusId": 2,
        "filingReferenceNumber": 100003526549,
    }


def _order_row_with_html() -> dict:
    """An order row whose documentBase64 is the HTML interstitial (no path)."""
    row = _order_row_with_pdf()
    row["caseNumber"] = "DC/525/CC/101/2023"
    row["documentBase64"] = _HTML_INTERSTITIAL
    row["orderDocumentPath"] = None
    return row


def _judgment_only_row() -> dict:
    """A row whose ORDER slot is empty but a JUDGMENT PDF is present."""
    return {
        "caseNumber": "DC/525/CC/1095/2020",
        "complainantName": "C. Complainant",
        "respondentName": "State Bank",
        "caseFilingDate": "2020-12-11",
        "orderDate": None,
        "documentBase64": None,
        "caseStageName": "DISPOSED OFF",
        "dateOfDisposal": "2022-03-16",
        "dateOfHearing": None,
        "judgemtmentDate": "2022-03-16",
        "judgmentOrderDocumentBase64": _REAL_PDF_B64,
        "judgemtmentDocumentPath": None,
        "filingReferenceNumber": 100002000111,
    }


def test_parse_case_maps_core_fields():
    c = parse_case(_order_row_with_pdf())
    assert c.cnr == "DC/525/CC/999/2024"
    assert c.title == "A. Complainant vs M/s. Respondent Pvt Ltd"
    assert c.stage == "DISPOSED OFF"
    assert c.filing_date and c.filing_date.isoformat() == "2024-03-13"
    assert c.next_hearing_date and c.next_hearing_date.isoformat() == "2024-11-29"


def test_additional_party_captured_from_nic_key():
    # additionalComplainantList entry keyed on `additional_respondent_name`
    # must NOT be dropped (fidelity fix).
    c = parse_case(_order_row_with_pdf())
    names = [p.name for p in c.parties]
    assert "B. Co-Complainant" in names


def test_real_base64_pdf_captured_inline():
    c = parse_case(_order_row_with_pdf())
    assert len(c.orders) == 1
    o = c.orders[0]
    assert o.inline_pdf_b64 == _REAL_PDF_B64
    # sanity: it really decodes to a PDF
    assert base64.b64decode(o.inline_pdf_b64).startswith(b"%PDF")
    assert o.order_date.isoformat() == "2024-11-29"


def test_html_interstitial_is_not_stored_as_pdf():
    # documentBase64 is raw HTML + no orderDocumentPath => no order at all
    # (never an OrderRef carrying HTML as if it were a PDF).
    c = parse_case(_order_row_with_html())
    assert c.orders == []


def test_judgment_only_row_yields_a_judgment_order():
    c = parse_case(_judgment_only_row())
    assert len(c.orders) == 1
    o = c.orders[0]
    assert o.inline_pdf_b64 == _REAL_PDF_B64
    assert o.order_date.isoformat() == "2022-03-16"  # from judgemtmentDate
    assert o.order_id.endswith("-judgment")


def test_order_and_judgment_both_present():
    row = _order_row_with_pdf()
    row["judgemtmentDate"] = "2024-12-01"
    row["judgmentOrderDocumentBase64"] = _REAL_PDF_B64
    c = parse_case(row)
    assert len(c.orders) == 2
    ids = {o.order_id for o in c.orders}
    assert any(i.endswith("-judgment") for i in ids)
    assert any(not i.endswith("-judgment") for i in ids)


def test_parse_case_stubs_shape():
    stubs = parse_case_stubs([_order_row_with_pdf(), _judgment_only_row()])
    assert [s.cnr for s in stubs] == ["DC/525/CC/999/2024", "DC/525/CC/1095/2020"]
    assert stubs[0].stage == "DISPOSED OFF"
