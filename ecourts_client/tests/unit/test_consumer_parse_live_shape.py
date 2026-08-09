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
    # DISPOSED OFF: dateOfHearing echoes the disposal date — not a future hearing.
    assert c.next_hearing_date is None


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
    # a PDF order carries no extracted HTML text
    assert o.order_text is None


def test_html_order_text_extracted_never_stored_as_pdf():
    # documentBase64 is the raw-HTML 'daily order' page (no PDF, no path):
    # the order is KEPT with its extracted text (not dropped, never a fake PDF).
    c = parse_case(_order_row_with_html())
    assert len(c.orders) == 1
    o = c.orders[0]
    assert o.inline_pdf_b64 is None          # never HTML-as-PDF
    assert o.order_text is not None
    assert "partly allowed" in o.order_text  # the disposition text survives
    assert "<html>" not in o.order_text      # tags stripped
    assert o.order_date.isoformat() == "2024-11-29"


def test_html_order_text_keeps_line_structure():
    # The consumer renders this text into the document the user reads, so block
    # elements must survive as line breaks — a space-joined judgment is a wall.
    c = parse_case(_order_row_with_html())
    text = c.orders[0].order_text
    assert "\n" in text
    lines = text.splitlines()
    assert "DIST CONSUMER COMMISSION, BANGALORE URBAN" in lines
    assert "partly allowed" in lines
    assert "" not in lines  # blank-line runs collapsed away


def test_long_order_text_is_not_silently_truncated():
    # Regression: the old 8000-char cap sliced NC/CC/2057/2016 mid-sentence and
    # dropped the operative directions. A full judgment must survive intact.
    body = "<p>" + ("The Commission finds as follows. " * 400) + "</p>"
    operative = "<p>the above consumer complaints stand disposed of</p>"
    row = _order_row_with_html()
    row["documentBase64"] = f"<html><body>{body}{operative}</body></html>"
    o = parse_case(row).orders[0]
    assert len(o.order_text) > 8000                       # cap no longer bites
    assert "stand disposed of" in o.order_text            # the tail SURVIVES
    assert "truncated" not in o.order_text                # and nothing was cut


def test_truncation_is_announced_when_it_does_happen():
    # The remaining cap is a runaway guard. If it ever fires, the document must
    # not look complete.
    from ecourts_client.consumer import parsers

    row = _order_row_with_html()
    row["documentBase64"] = "<html><body><p>" + ("x" * (parsers._MAX_ORDER_TEXT + 50)) + "</p></body></html>"
    o = parse_case(row).orders[0]
    assert o.order_text.endswith(parsers._TRUNCATION_MARKER)


def test_identical_order_and_judgment_collapse_to_one():
    # Live on NC/CC/2057/2016: e-Jagriti puts the SAME 16k HTML judgment in both
    # documentBase64 and judgmentOrderDocumentBase64 with the same date. The user
    # must see it once, labelled as the judgment.
    row = _order_row_with_html()
    row["judgemtmentDate"] = row["orderDate"]
    row["judgmentOrderDocumentBase64"] = row["documentBase64"]
    orders = parse_case(row).orders
    assert len(orders) == 1
    assert orders[0].order_id.endswith("-judgment")


def test_distinct_order_and_judgment_are_both_kept():
    # Dedup must key on CONTENT, not on "both slots populated" — a genuine daily
    # order plus a separate judgment is two real documents.
    row = _order_row_with_html()
    row["judgemtmentDate"] = "2024-12-20"
    row["judgmentOrderDocumentBase64"] = _REAL_PDF_B64
    orders = parse_case(row).orders
    assert len(orders) == 2
    assert {o.order_id.endswith("-judgment") for o in orders} == {True, False}

def test_html_order_text_strips_a_server_error_banner():
    """A commission's order page can carry a PHP error banner in its body (live
    on tdsat.gov.in; the same NIC/PHP stack serves e-Jagriti's order views). The
    banner is page furniture and must not ride through into the order text."""
    row = _order_row_with_html()
    row["documentBase64"] = (
        "<html><body><b>DIST CONSUMER COMMISSION, BANGALORE URBAN</b>"
        "<p>partly allowed</p>"
        "<b>Warning</b> :  include(): Failed opening 'orderp.php' for inclusion "
        "in /home/www/html/order.php on line 22"
        "</body></html>"
    )
    o = parse_case(row).orders[0]
    assert o.order_text.splitlines() == [
        "DIST CONSUMER COMMISSION, BANGALORE URBAN",
        "partly allowed",
    ]
    assert "Warning" not in o.order_text
    assert "on line 22" not in o.order_text


def test_html_order_text_keeps_a_court_notice():
    """False-positive guard: 'Notice:' is ordinary order language, and stripping
    on the bare word would delete the operative part of the order."""
    row = _order_row_with_html()
    row["documentBase64"] = (
        "<html><body><p>ORDER Notice: issue notice to the opposite party "
        "returnable on 12.09.2026.</p></body></html>"
    )
    o = parse_case(row).orders[0]
    assert "issue notice to the opposite party returnable on 12.09.2026." in o.order_text


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
