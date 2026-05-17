"""PDF fetching for order documents + HC cause-list PDFs.

The eCourts API returns order PDFs via display_pdf.php URLs embedded in the
caseHistoryWebService response (e.g. in the `last_order` and `interimOrder`
HTML tables). These URLs are absolute, signed (via `params=` and `authtoken=`
query string), and served as application/pdf.

We use the same authenticated requests.Session that fetched the case so the
JWT-encrypted Authorization header is reused. The URL signature is what really
authorizes the download; the Bearer header may not be strictly required, but
sending it matches what the mobile app does.

Bombay HC (and likely others) sometimes serve PDFs with leading CRLF bytes
before the `%PDF-` magic header -- a stray HTTP framing artifact from the
upstream server. PDF spec section 7.5.2 calls for `%PDF-` as the very first
bytes of the file, so a strict validator would reject those. In practice
PDF parsers (Adobe Reader, pdfplumber, qpdf) tolerate up to ~1024 leading
junk bytes by scanning forward for the magic header. We do the same.
"""
from __future__ import annotations

import requests

from ecourts_client.errors import PDFInvalid, PDFNotFound


# Allow up to this many leading bytes before the %PDF- magic header. Real
# leading-junk we've observed is 2 bytes (CRLF); 1024 is generous enough to
# handle BOMs, additional framing, and HTML error pages with a misleading
# "%PDF" mention while still bounding the scan.
_MAGIC_HEADER_SCAN_LIMIT = 1024


def fetch_pdf(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=60, stream=False)
    if resp.status_code == 404:
        raise PDFNotFound(f"404 for {url}")
    if 400 <= resp.status_code < 600:
        raise PDFNotFound(f"{resp.status_code} for {url}")

    content = resp.content
    if content.startswith(b"%PDF"):
        return content

    # Tolerate leading whitespace / framing bytes before %PDF- (Bombay HC
    # serves CRLF-prefixed PDFs in its causelist_pdf.php responses). Scan
    # the first KB for the magic header; if found, return content from
    # that offset onward so downstream consumers (pdfplumber, file writes)
    # see a clean PDF byte stream.
    head = content[:_MAGIC_HEADER_SCAN_LIMIT]
    pdf_start = head.find(b"%PDF")
    if pdf_start > 0:
        return content[pdf_start:]

    raise PDFInvalid(
        f"non-PDF body at {url}: starts with {content[:8]!r}"
    )
