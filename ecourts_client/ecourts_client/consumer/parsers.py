"""Parse e-Jagriti JSON payloads into the shared transport models.

Schema-tolerant on purpose (undocumented NIC API): coerce/guard every field so
a stringy/renamed/null value degrades gracefully instead of crashing mid-fetch,
and honor the API's misspelled keys (``additionalRespondantList``,
``judgemtmentDocumentPath``). See ``docs/spike-ejagriti-transport.md``.
"""
from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from typing import Any

from ecourts_client.consumer.models import CommissionRef
from ecourts_client.errors import SchemaChanged
from ecourts_client.models import Case, CaseStub, OrderRef, Party
from ecourts_client.parsers.disposal import reads_as_disposed


def _opt_str(value: Any) -> str | None:
    """Coerce to a stripped non-empty str, or None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_date(value: Any) -> date | None:
    """Best-effort date parse: ISO (with/without time) then dd-MM-yyyy / dd/MM/yyyy."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_commissions(data: Any) -> list[CommissionRef]:
    """Parse the state/district commission lister payload into CommissionRefs."""
    if not isinstance(data, list):
        raise SchemaChanged("data", f"commission list not a list: {type(data).__name__}")
    out: list[CommissionRef] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            cid = int(row["commissionId"])
        except (KeyError, TypeError, ValueError):
            continue  # schema-tolerant: skip a malformed/renamed row, don't crash
        name = _str(row.get("commissionNameEn") or row.get("commissionName"))
        out.append(
            CommissionRef(
                commission_id=cid,
                name=name,
                is_bench=bool(row.get("circuitAdditionBenchStatus")),
                active=bool(row.get("activeStatus", True)),
            )
        )
    return out


def _extra_name(extra: Any) -> str:
    if isinstance(extra, str):
        return extra.strip()
    if isinstance(extra, dict):
        # Live rows carry additional parties as
        # ``{"additional_respondent_name": "..."}`` (the SAME key is used inside
        # both additionalComplainantList and additionalRespondantList — a NIC
        # quirk), so check those first, then the generic fallbacks.
        for k in (
            "additional_complainant_name",
            "additional_respondent_name",
            "name",
            "complainantName",
            "respondentName",
            "partyName",
        ):
            v = extra.get(k)
            if v:
                return _str(v)
    return ""


def _title(row: dict[str, Any]) -> str:
    comp = _str(row.get("complainantName"))
    resp = _str(row.get("respondentName"))
    if comp and resp:
        return f"{comp} vs {resp}"
    return comp or resp or _str(row.get("caseNumber"))


def parse_case_stubs(data: Any, *, court: str = "") -> list[CaseStub]:
    """Parse getCaseDetailsBySearchType rows into lightweight CaseStubs.

    The Consumer identity slot (``CaseStub.cnr``) carries the e-Jagriti case
    number — Consumer has no eCourts CNR; callers map it to ``forum_case_ref``.
    """
    if not isinstance(data, list):
        raise SchemaChanged("data", f"case list not a list: {type(data).__name__}")
    stubs: list[CaseStub] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        case_no = _str(row.get("caseNumber"))
        if not case_no:
            continue
        stubs.append(
            CaseStub(
                cnr=case_no,
                title=_title(row),
                case_number=case_no,
                court=court,
                stage=_opt_str(row.get("caseStageName")),
            )
        )
    return stubs


def _pdf_b64_or_none(value: Any) -> str | None:
    """Return a base64 string ONLY if it decodes to a real PDF, else None.

    e-Jagriti's ``documentBase64`` / ``judgmentOrderDocumentBase64`` are
    POLYMORPHIC (live-confirmed 2026-07-04): ~half the populated values are a
    genuine base64-encoded PDF, the rest are an HTML "order not available /
    login" interstitial — sometimes a *raw* ``<html>…`` string that isn't even
    base64. Storing those as an inline PDF would archive garbage, so validate by
    decoding and checking the ``%PDF`` magic. Returns the normalized base64
    (whitespace-stripped) so the caller stores a clean payload.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s or s[:1] == "<":  # raw HTML interstitial, not base64
        return None
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None
    # NIC/nginx may prepend CRLF/BOM before %PDF- — scan the first 1KB.
    if b"%PDF" not in raw[:1024]:
        return None
    return s


# A whole judgment, not a snippet. This text is the ONLY copy of an HTML-only
# order — e-Jagriti serves no PDF for it and no re-fetchable URL — so the
# consumer renders a document straight from this string. The old 8000 cap
# silently sliced NC/CC/2057/2016 mid-sentence and threw away the operative
# directions ("...the above consumer complaints stand disposed of"), which is
# precisely the part a user opens an order to read. The limit that remains is a
# runaway guard, not an editorial one, and truncation is now ANNOUNCED — a
# lossy document must never look complete.
_MAX_ORDER_TEXT = 200_000
_TRUNCATION_MARKER = "\n\n[... order text truncated ...]"

# Tags that imply a line break in the rendered page. e-Jagriti's order pages are
# table-based (one <td> per line), so without these every judgment collapses
# into a single unreadable paragraph.
_BLOCK_SEPARATOR = "\n"


def _html_order_text(value: Any) -> str | None:
    """Extract readable text from an HTML 'daily order' page, else None.

    The other half of the polymorphic ``documentBase64`` (see
    ``_pdf_b64_or_none``): a raw ``<html>…`` page that IS the order text (court
    header, parties, then the disposition e.g. "partly allowed"). Rather than
    drop it, pull the visible text so the order still surfaces on the timeline
    (date + text). Uses the stdlib ``html.parser`` (no lxml dependency) and is
    defensive — any parse failure degrades to None, never crashing the fetch.
    Only treats a RAW ``<…`` string as HTML (a base64 PDF is handled upstream).

    ★ Line structure is PRESERVED (block elements separate with a newline, runs
    of blank lines collapse). Callers render this to a document the user reads,
    and a space-joined judgment is a wall of text nobody can follow."""
    if not value:
        return None
    s = str(value).strip()
    if s[:1] != "<":  # not raw HTML (base64/empty handled elsewhere)
        return None
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(s, "html.parser").get_text(_BLOCK_SEPARATOR, strip=True)
    except Exception:
        import re

        text = re.sub(r"<[^>]+>", _BLOCK_SEPARATOR, s)
    # Collapse horizontal runs per line, then squeeze blank-line runs to one.
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    text = "\n".join(out).strip()
    if not text:
        return None
    if len(text) > _MAX_ORDER_TEXT:
        return text[:_MAX_ORDER_TEXT] + _TRUNCATION_MARKER
    return text


def _one_order(
    *, order_date: date | None, b64: Any, path: Any, order_id: str
) -> OrderRef | None:
    """Build one OrderRef from a (date, inline-b64, path) triple, or None.

    The ``b64`` field is POLYMORPHIC (see ``_pdf_b64_or_none``): a real base64
    PDF → ``inline_pdf_b64``; an HTML 'daily order' page → its text extracted to
    ``order_text``. Kept when it has a date AND at least one of: a valid inline
    PDF, a document path, or extracted HTML text — so HTML-only orders surface
    (date + disposition text) instead of being silently dropped."""
    if not order_date:
        return None
    pdf_b64 = _pdf_b64_or_none(b64)
    # If it wasn't a PDF, the same field may be the order-text HTML page.
    order_text = None if pdf_b64 else _html_order_text(b64)
    path_str = _str(path)
    if not pdf_b64 and not path_str and not order_text:
        return None
    return OrderRef(
        order_date=order_date,
        order_url=path_str,
        order_id=order_id,
        inline_pdf_b64=pdf_b64,
        order_text=order_text,
    )


def _same_document(a: OrderRef, b: OrderRef) -> bool:
    """True when two OrderRefs carry the SAME underlying document.

    e-Jagriti frequently populates ``documentBase64`` and
    ``judgmentOrderDocumentBase64`` with a BYTE-IDENTICAL payload — live on
    NC/CC/2057/2016, where both fields are the same 16,446-char HTML judgment and
    ``orderDate == judgemtmentDate == dateOfDisposal``. Emitting both would show
    the user the same judgment twice (and store it twice on disk), so identical
    content on the same date collapses to one order."""
    if a.order_date != b.order_date:
        return False
    if a.inline_pdf_b64 or b.inline_pdf_b64:
        return a.inline_pdf_b64 == b.inline_pdf_b64
    if a.order_text or b.order_text:
        return a.order_text == b.order_text
    return bool(a.order_url) and a.order_url == b.order_url


def _parse_orders(row: dict[str, Any], case_no: str) -> list[OrderRef]:
    """Up to two OrderRefs per row: the daily ORDER and the final JUDGMENT.

    e-Jagriti carries these on DIFFERENT keys (``orderDate``/``documentBase64``
    vs ``judgemtmentDate``[sic]/``judgmentOrderDocumentBase64``), so a
    judgment-only row (``orderDate`` null) previously lost its document entirely.
    Each PDF arrives INLINE as base64 (validated → ``OrderRef.inline_pdf_b64``)
    or as a document path (``order_url``). The base id is the filing reference so
    the two refs stay distinct.

    ★ When both slots turn out to be the SAME document (see ``_same_document``)
    the JUDGMENT ref wins and the daily-order duplicate is dropped: it is the
    accurate label for the artifact, and ``-judgment`` is already the id every
    stored Consumer order row carries in prod, so dedup can't orphan one.
    """
    ref_id = _str(row.get("filingReferenceNumber")) or case_no
    out: list[OrderRef] = []
    order = _one_order(
        order_date=_parse_date(row.get("orderDate")),
        b64=row.get("documentBase64"),
        path=row.get("orderDocumentPath"),
        order_id=ref_id,
    )
    if order:
        out.append(order)
    judgment = _one_order(
        order_date=_parse_date(row.get("judgemtmentDate")),  # [sic] NIC misspelling
        b64=row.get("judgmentOrderDocumentBase64"),
        path=row.get("judgemtmentDocumentPath"),  # [sic]
        order_id=f"{ref_id}-judgment",
    )
    if judgment:
        out = [o for o in out if not _same_document(o, judgment)]
        out.append(judgment)
    return out


def parse_case(row: dict[str, Any], *, court: str = "") -> Case:
    """Parse a single getCaseDetailsBySearchType row into a generic ``Case``.

    ``Case.cnr`` holds the e-Jagriti case number (the forum identity slot). The
    row is a status snapshot, so ``history`` is left empty; the timeline is
    carried by ``filing_date`` / ``next_hearing_date`` / ``orders``.
    """
    case_no = _str(row.get("caseNumber"))

    parties: list[Party] = []
    comp = _str(row.get("complainantName"))
    if comp:
        parties.append(
            Party(name=comp, role="complainant", advocate=_opt_str(row.get("complainantAdvocateName")))
        )
    resp = _str(row.get("respondentName"))
    if resp:
        parties.append(
            Party(name=resp, role="respondent", advocate=_opt_str(row.get("respondentAdvocateName")))
        )
    for extra in row.get("additionalComplainantList") or []:
        nm = _extra_name(extra)
        if nm:
            parties.append(Party(name=nm, role="complainant"))
    for extra in row.get("additionalRespondantList") or []:  # [sic] NIC misspelling
        nm = _extra_name(extra)
        if nm:
            parties.append(Party(name=nm, role="respondent"))

    stage = _str(row.get("caseStageName"))
    # A disposed consumer case echoes the last hearing / disposal date into
    # ``dateOfHearing``. Null it on disposal — detected from the stage verb
    # ("DISPOSED OFF") or a populated ``dateOfDisposal`` (covers a "Reserved for
    # Orders" stage where only the disposal date reveals the case is decided).
    next_hearing = _parse_date(row.get("dateOfHearing"))
    if next_hearing is not None and reads_as_disposed(
        stage=stage,
        next_hearing_date=next_hearing,
        decision_date=_parse_date(row.get("dateOfDisposal")),
    ):
        next_hearing = None

    return Case(
        cnr=case_no,
        title=_title(row),
        court=court,
        stage=stage,
        next_hearing_date=next_hearing,
        # NOTE: 'judgeName' is not confirmed in the row schema (spike §8 defers
        # to a live populated-row fixture); stays None when absent.
        judge=_opt_str(row.get("judgeName")),
        parties=parties,
        history=[],
        orders=_parse_orders(row, case_no),
        filing_date=_parse_date(row.get("caseFilingDate")),
    )
