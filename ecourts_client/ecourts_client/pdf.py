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

import logging

import requests

from ecourts_client.errors import PDFInvalid, PDFNotFound

logger = logging.getLogger(__name__)


# eCourts v4.0 order PDFs are obtained in TWO steps: POST ``display_pdf_new.php``
# (encrypted params on the query string) returns
# ``{"pdf_url": "https://csc.ecourts.gov.in/.../<hash>.pdf"}``, then GET that
# signed alias URL. The second URL is unreachable without the first call, so the
# parser encodes the order's params behind this scheme and ``fetch_order_pdf``
# resolves it through the authenticated Session.
_V4_ORDER_SCHEME = "displaypdf:"
_V4_ORDER_FIELDS = ("filename", "caseno", "cCode", "appFlag", "state_cd", "dist_cd", "court_code")


def encode_v4_order(row: dict) -> str:
    """Encode a v4 order dict into a ``displaypdf:`` order_url for fetch_order_pdf.

    Missing fields are still sent as empty strings -- behaviour is deliberately
    unchanged -- but they are now reported. ``display_pdf_new.php`` answers HTTP
    200 with a 14-byte ``'Invalid Input4'`` for some orders (all Delhi HC so
    far), which is neither the ~228-byte throttle page nor the verbose
    "order is not uploaded for - case no- X" that means the document does not
    exist. A terse indexed error reads like parameter validation, and this
    function is the one place a blank parameter can originate: the only upstream
    guard checks ``filename`` and ``order_date`` (parsers/case_history.py:214),
    while High Court and District share one parser, so an HC row shaped
    differently from the district row this field list came from silently yields
    a blank.

    Not raising or skipping: we do not yet know which of the seven eCourts
    actually requires, and guessing would break orders that work today.
    """
    missing = [
        k for k in _V4_ORDER_FIELDS if not str(row.get(k, "") or "").strip()
    ]
    if missing:
        logger.warning(
            "ECOURTS_ORDER_FIELDS_MISSING fields=%s filename=%r caseno=%r",
            ",".join(missing), row.get("filename"), row.get("caseno"),
        )
    return _V4_ORDER_SCHEME + urlencode({k: str(row.get(k, "")) for k in _V4_ORDER_FIELDS})


def _wire_caseno(caseno: str | None) -> str:
    """``caseno`` as ``display_pdf_new.php`` will accept it.

    NIC's validator rejects a hyphen: sending
    ``C.A.(COMM.IPD-TM)/0000049/2026`` answers HTTP 200 with the 14-byte body
    ``Invalid Input4``, while the same request with the hyphen removed returns
    a signed ``pdf_url`` for the correct document (verified live 2026-07-31 on
    DLHC010320362026 -- the recovered PDF is the real 23-07-2026 order).

    This is not cosmetic: every Delhi HC Intellectual-Property-Division case
    type is hyphenated (``C.A.(COMM.IPD-TM)``, ``W.C.(C)-IPD``, ``RFA(OS)-IPD``,
    ``CRP-IPD``, ...), so without this NO IPD order PDF is reachable at all.

    Only the hyphen is removed. The zero-padded number is load-bearing --
    un-padding it reproduces ``Invalid Input4`` -- and ``filename`` is what
    actually identifies the document, so this cannot select a different one.
    """
    return (caseno or "").replace("-", "")


def fetch_order_pdf(session, url: str) -> bytes:
    """Fetch an order PDF via the authenticated ``session``.

    v4 orders (``displaypdf:`` scheme) POST ``display_pdf_new.php`` for a signed
    ``pdf_url`` then GET it; legacy v3 orders are a direct signed GET.

    ``caseno`` is sanitised here rather than in ``encode_v4_order`` so that the
    ``displaypdf:`` URLs already persisted in ``case_orders.order_url`` start
    working without a backfill, and so the stored value stays the true case
    number.
    """
    if url.startswith(_V4_ORDER_SCHEME):
        params = dict(parse_qsl(url[len(_V4_ORDER_SCHEME):]))
        if "caseno" in params:
            params["caseno"] = _wire_caseno(params["caseno"])
        params["bilingual_flag"] = "1"
        session._ensure_jwt()
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
    # Gate PDF egress on the SAME shared schedule as the JSON API. The JSON path
    # paces every wire call in ``_session._send``, but this GET (the order-alias
    # download + every cause-list PDF) bypasses ``_send`` entirely, so a bulk
    # cause-list/order PDF burst could trip the per-IP 405 throttle that the
    # Tier-2 limiter exists to prevent. Import lazily to avoid any
    # ``_session`` <-> ``pdf`` import cycle (mirrors redis_limiter's lazy import).
    from ecourts_client._session import _get_rate_gate, _log_throttle, _penalize_rate_gate

    _get_rate_gate().wait()
    resp = session.get(url, timeout=60, stream=False)
    if resp.status_code == 404:
        raise PDFNotFound(f"404 for {url}")
    if resp.status_code == 405:
        # Same burst-throttle signal as the JSON path (HTTP 405 + HTML "Search
        # Page not Found", ~15-30 min per-IP ban). Record it -- otherwise PDF-path
        # 405s are invisible to the throttle counter -- and widen the shared
        # limiter so the whole fleet backs off. Surface as PDFNotFound so caller
        # control flow is unchanged (a missing PDF is already skippable).
        _log_throttle("throttle_405", "fetch_pdf", 405)
        _penalize_rate_gate()
        raise PDFNotFound(f"405 (burst throttle) for {url}")
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
