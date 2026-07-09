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

from urllib.parse import parse_qsl, urlencode

import requests

from ecourts_client.errors import PDFInvalid, PDFNotFound


# eCourts v4.0 order PDFs are obtained in TWO steps: POST ``display_pdf_new.php``
# (encrypted params on the query string) returns
# ``{"pdf_url": "https://csc.ecourts.gov.in/.../<hash>.pdf"}``, then GET that
# signed alias URL. The second URL is unreachable without the first call, so the
# parser encodes the order's params behind this scheme and ``fetch_order_pdf``
# resolves it through the authenticated Session.
_V4_ORDER_SCHEME = "displaypdf:"
_V4_ORDER_FIELDS = ("filename", "caseno", "cCode", "appFlag", "state_cd", "dist_cd", "court_code")


def encode_v4_order(row: dict) -> str:
    """Encode a v4 order dict into a ``displaypdf:`` order_url for fetch_order_pdf."""
    return _V4_ORDER_SCHEME + urlencode({k: str(row.get(k, "")) for k in _V4_ORDER_FIELDS})


def fetch_order_pdf(session, url: str) -> bytes:
    """Fetch an order PDF via the authenticated ``session``.

    v4 orders (``displaypdf:`` scheme) POST ``display_pdf_new.php`` for a signed
    ``pdf_url`` then GET it; legacy v3 orders are a direct signed GET.
    """
    if url.startswith(_V4_ORDER_SCHEME):
        params = dict(parse_qsl(url[len(_V4_ORDER_SCHEME):]))
        params["bilingual_flag"] = "1"
        if session.jwt is None:
            session.init()
        resp = session._send("display_pdf_new.php", params, with_bearer=True, method="POST")
        pdf_url = resp.get("pdf_url")
        if not pdf_url:
            raise PDFNotFound(f"display_pdf_new.php returned no pdf_url: {resp!r}")
        return fetch_pdf(session._http, pdf_url)
    return fetch_pdf(session._http, url)


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
