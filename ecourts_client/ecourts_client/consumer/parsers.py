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


def _one_order(
    *, order_date: date | None, b64: Any, path: Any, order_id: str
) -> OrderRef | None:
    """Build one OrderRef from a (date, inline-b64, path) triple, or None.

    Kept only if it carries a real PDF (validated inline base64) OR a document
    path, AND has a date to place it on the timeline. Invalid/HTML inline blobs
    are dropped to None so a bogus 'order' never appears."""
    if not order_date:
        return None
    pdf_b64 = _pdf_b64_or_none(b64)
    path_str = _str(path)
    if not pdf_b64 and not path_str:
        return None
    return OrderRef(
        order_date=order_date,
        order_url=path_str,
        order_id=order_id,
        inline_pdf_b64=pdf_b64,
    )


def _parse_orders(row: dict[str, Any], case_no: str) -> list[OrderRef]:
    """Up to two OrderRefs per row: the daily ORDER and the final JUDGMENT.

    e-Jagriti carries these on DIFFERENT keys (``orderDate``/``documentBase64``
    vs ``judgemtmentDate``[sic]/``judgmentOrderDocumentBase64``), so a
    judgment-only row (``orderDate`` null) previously lost its document entirely.
    Each PDF arrives INLINE as base64 (validated → ``OrderRef.inline_pdf_b64``)
    or as a document path (``order_url``). The base id is the filing reference so
    the two refs stay distinct.
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

    return Case(
        cnr=case_no,
        title=_title(row),
        court=court,
        stage=_str(row.get("caseStageName")),
        next_hearing_date=_parse_date(row.get("dateOfHearing")),
        # NOTE: 'judgeName' is not confirmed in the row schema (spike §8 defers
        # to a live populated-row fixture); stays None when absent.
        judge=_opt_str(row.get("judgeName")),
        parties=parties,
        history=[],
        orders=_parse_orders(row, case_no),
        filing_date=_parse_date(row.get("caseFilingDate")),
    )
