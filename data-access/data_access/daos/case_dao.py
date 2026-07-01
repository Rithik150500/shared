"""DAO for the unified cases table."""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from data_access.models.case import Case
from data_access.models.case_preferences import CasePreferences
from ecourts_client.models import Case as DataCase
from ecourts_client.routing import classify_cnr


# Columns whose change should bump last_change_at. Only fields present in
# `_case_to_row_payload` participate in diff; "orders" lives in the relationship
# table (case_orders) and is intentionally excluded — order-level diffing belongs
# in order_dao, not here.
_DIFFABLE_FIELDS = ("stage", "case_status", "next_hearing_date", "judge", "court", "history")

# A-4 audit fix: defense-in-depth CNR validation at every DAO write entry.
# Handler-level _CNR_RE (handlers/monitoring/save_command.py) is the first
# line of defense but non-handler write paths (backfill, scheduler workers,
# future refactors) bypass it. Without this assertion any caller could
# persist garbage CNRs into the cases table.
#
# Pattern: 16 chars total — 4 alpha (state + court code) + 12 alphanumeric.
# Mirrors ecourts_client.routing.CNR_REGEX but kept local so DAO callers
# get a plain ValueError instead of CNRMalformed (no ecourts_client import
# in the contract of writes here).
_CNR_REGEX = re.compile(r"^[A-Z]{2}[A-Z]{2}[A-Z0-9]{12}$")


def _assert_valid_cnr(cnr: str) -> None:
    """Raise ValueError if `cnr` is not a syntactically valid 16-char CNR."""
    if not isinstance(cnr, str) or not _CNR_REGEX.match(cnr):
        raise ValueError(
            f"Invalid CNR {cnr!r}: must match {_CNR_REGEX.pattern}",
        )


# --- Multi-forum support -------------------------------------------------
# Forums served by the eCourts adapters — the only forums that carry a 16-char
# CNR. Every other forum keys on forum_case_ref alone and has a NULL cnr/portal.
_ECOURTS_FORUMS = frozenset(("ecourts_district", "ecourts_highcourt"))

# Sources an automated adapter can refresh. Manual rows are excluded from
# get_due_for_refresh so the scheduler never fetches them (and never calls
# portal_class_from_cnr on their NULL cnr).
_AUTO_SOURCES = frozenset(("ecourts_auto", "ejagriti_auto", "drt_auto", "sc_auto"))

# forum_case_ref for non-eCourts forums. Real Indian case numbers are messy —
# e.g. "SLP(C) 12345/2026", "O.A. No. 77 of 2025", "CC/512/2024" — so allow
# letters, digits, spaces and the punctuation those formats use: / - . , ( ) &
_REF_REGEX = re.compile(r"^[A-Za-z0-9/\-.,()&\s]{1,128}$")


def _ecourts_forum_for_cnr(cnr: str) -> str:
    """Map a CNR to its eCourts forum value (district/highcourt) via classify_cnr."""
    return "ecourts_highcourt" if classify_cnr(cnr) == "highcourt" else "ecourts_district"


def _assert_valid_ref(forum: str, *, cnr: str | None, forum_case_ref: str) -> None:
    """Validate identity for any forum without weakening the eCourts CNR check.

    eCourts forums require a valid 16-char CNR and forum_case_ref == cnr (so the
    partial-cnr and (forum, ref) unique keys stay aligned). Non-eCourts forums
    must NOT carry a CNR and take a permissive free-form ref.
    """
    if forum in _ECOURTS_FORUMS:
        if cnr is None:
            raise ValueError(f"forum={forum!r} requires a CNR")
        _assert_valid_cnr(cnr)
        if forum_case_ref != cnr:
            raise ValueError("eCourts forum_case_ref must equal cnr")
    else:
        if cnr is not None:
            raise ValueError(f"forum={forum!r} must not carry a CNR")
        if not isinstance(forum_case_ref, str) or not _REF_REGEX.match(forum_case_ref):
            raise ValueError(
                f"Invalid forum_case_ref {forum_case_ref!r}: "
                f"must match {_REF_REGEX.pattern}",
            )


def upsert_case(
    s: Session,
    *,
    user_id: uuid.UUID,
    cnr: str,
    case_data: DataCase,
    client_id: str | None = None,
    refresh_enabled: bool = True,
    notes: str | None = None,
    last_refreshed_at: datetime | None = None,
) -> Case:
    """Insert-or-update a case row from the shared `Case` dataclass."""
    _assert_valid_cnr(cnr)
    existing = get_by_cnr(s, user_id=user_id, cnr=cnr)
    payload = _case_to_row_payload(case_data)
    payload["user_id"] = user_id
    payload["cnr"] = cnr
    # eCourts identity: forum_case_ref mirrors the authoritative cnr param
    # (payload already carries forum/source from _case_to_row_payload).
    payload["forum_case_ref"] = cnr
    payload["client_id"] = client_id
    payload["refresh_enabled"] = refresh_enabled
    payload["notes"] = notes
    payload["last_refreshed_at"] = last_refreshed_at or datetime.now(timezone.utc)
    payload["updated_at"] = datetime.now(timezone.utc)
    if existing is None:
        row = Case(**payload)
        s.add(row)
        s.flush()
        return row
    for k, v in payload.items():
        setattr(existing, k, v)
    s.flush()
    return existing


def get_by_cnr(s: Session, *, user_id: uuid.UUID, cnr: str) -> Case | None:
    stmt = select(Case).where(Case.user_id == user_id, Case.cnr == cnr)
    return s.execute(stmt).scalar_one_or_none()


def get_by_ref(
    s: Session, *, user_id: uuid.UUID, forum: str, forum_case_ref: str,
) -> Case | None:
    """Forum-neutral lookup by the universal (user_id, forum, forum_case_ref) key."""
    stmt = select(Case).where(
        Case.user_id == user_id,
        Case.forum == forum,
        Case.forum_case_ref == forum_case_ref,
    )
    return s.execute(stmt).scalar_one_or_none()


def list_by_user(s: Session, *, user_id: uuid.UUID, limit: int = 200) -> list[Case]:
    stmt = (
        select(Case)
        .where(Case.user_id == user_id)
        .order_by(Case.created_at.desc())
        .limit(limit)
    )
    return list(s.execute(stmt).scalars())


def exists(s: Session, *, user_id: uuid.UUID, cnr: str) -> bool:
    return get_by_cnr(s, user_id=user_id, cnr=cnr) is not None


def get_due_for_refresh(s: Session, *, limit: int = 100) -> list[Case]:
    """Cases needing refresh, ordered NULLS FIRST then oldest first.

    Multi-forum: only rows with an automated `source` and a non-NULL `cnr` are
    returned. This keeps manual / arbitration rows out of the poll (regardless of
    refresh_enabled) and guarantees the scheduler never calls classify_cnr on a
    NULL cnr. As Phase-2/3 auto sources land (ejagriti_auto/drt_auto/sc_auto),
    the scheduler's per-row portal classification must branch on `forum` first.
    """
    stmt = (
        select(Case)
        .where(
            Case.refresh_enabled.is_(True),
            Case.source.in_(_AUTO_SOURCES),
            Case.cnr.isnot(None),
        )
        .order_by(Case.last_refreshed_at.asc().nullsfirst())
        .limit(limit)
    )
    return list(s.execute(stmt).scalars())


def diff_and_update(
    s: Session,
    *,
    user_id: uuid.UUID,
    cnr: str,
    case_data: DataCase,
) -> list[str]:
    """Apply fresh fetch, returning the names of columns whose value changed."""
    _assert_valid_cnr(cnr)
    existing = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if existing is None:
        upsert_case(s, user_id=user_id, cnr=cnr, case_data=case_data)
        return list(_DIFFABLE_FIELDS)

    fresh_payload = _case_to_row_payload(case_data)
    changes: list[str] = []
    for field in _DIFFABLE_FIELDS:
        new_val = fresh_payload.get(field)
        if new_val != getattr(existing, field):
            setattr(existing, field, new_val)
            changes.append(field)

    if changes:
        existing.last_change_at = datetime.now(timezone.utc)

    existing.last_refreshed_at = datetime.now(timezone.utc)
    existing.raw_response = fresh_payload["raw_response"]
    existing.updated_at = datetime.now(timezone.utc)
    s.flush()
    return changes


# Step-3: columns the casepilot gap-writes may partial-patch onto a Case.
# Mirrors the legacy SQLite update_case whitelist that has no DataCase to diff
# against. Kept SEPARATE from _DIFFABLE_FIELDS (which drives the full-fetch
# differ); update_fields is a direct setter, not a re-diff.
_UPDATABLE_FIELDS = frozenset((
    "case_number", "title", "case_status", "stage", "next_hearing_date",
    "judge", "court", "portal", "refresh_enabled", "notes",
    "last_refreshed_at", "last_change_at",
    "case_detail_json", "case_detail_md", "mini_case_detail_md",
    # Multi-forum columns (manual patch + Phase-2/3 adapter writes).
    "forum", "forum_case_ref", "source", "filing_year",
))

# Subset whose change should bump last_change_at (parity with _DIFFABLE_FIELDS).
_UPDATABLE_DIFFABLE = frozenset(("stage", "case_status", "next_hearing_date", "judge", "court"))


def update_fields(s: Session, *, user_id: uuid.UUID, cnr: str, **cols: Any) -> list[str]:
    """Partial column patch on the (user_id, cnr) Case row over an explicit
    allow-list. Returns the list of columns whose value actually changed.

    Unlike diff_and_update (full-fetch differ keyed on a DataCase), this is a
    direct setter the casepilot gap-writes use to mirror their SQLite update_case
    whitelist — including the Step-3 detail blobs + notes, which are NOT in
    _DIFFABLE_FIELDS. Raises ValueError on an unknown column. Missing row -> [].
    """
    _assert_valid_cnr(cnr)
    unknown = set(cols) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_fields: unknown column(s) {sorted(unknown)}")
    row = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if row is None:
        return []
    changed: list[str] = []
    for k, v in cols.items():
        if getattr(row, k) != v:
            setattr(row, k, v)
            changed.append(k)
    if any(c in _UPDATABLE_DIFFABLE for c in changed):
        row.last_change_at = datetime.now(timezone.utc)
    if changed:
        row.updated_at = datetime.now(timezone.utc)
    s.flush()
    return changed


def mark_cnr_not_found(s: Session, *, user_id: uuid.UUID, cnr: str) -> Case:
    """Insert a tombstone row for a CNR that eCourts reports as not-found.

    `refresh_enabled=False` so the scheduler skips it; portal inferred from CNR.
    """
    _assert_valid_cnr(cnr)
    existing = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if existing is not None:
        existing.refresh_enabled = False
        existing.updated_at = datetime.now(timezone.utc)
        s.flush()
        return existing
    row = Case(
        user_id=user_id,
        cnr=cnr,
        portal=classify_cnr(cnr),
        forum=_ecourts_forum_for_cnr(cnr),
        forum_case_ref=cnr,
        source="ecourts_auto",
        refresh_enabled=False,
        raw_response={"cnr_not_found": True},
    )
    s.add(row)
    s.flush()
    return row


def toggle_refresh(s: Session, *, user_id: uuid.UUID, cnr: str) -> bool:
    """Toggle ``Case.refresh_enabled`` and return the new value.

    B.5b: Postgres replacement for the SQLite-side ``toggle_refresh`` hook.
    Raises ``LookupError`` if no case exists for the given (user_id, cnr) —
    callers (handlers/scheduler) already expect a hard failure here because
    the legacy SQLite implementation raised on a missing row too.
    """
    _assert_valid_cnr(cnr)
    row = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if row is None:
        raise LookupError(
            f"toggle_refresh: case not found for user_id={user_id} cnr={cnr!r}"
        )
    row.refresh_enabled = not row.refresh_enabled
    row.updated_at = datetime.now(timezone.utc)
    s.flush()
    return row.refresh_enabled


def mark_first_ndoh_email_sent(s: Session, *, user_id: uuid.UUID, cnr: str) -> None:
    """Stamp ``Case.first_ndoh_email_sent_at`` to now (UTC).

    B.5b: Postgres replacement for the SQLite ``mark_first_ndoh_email_sent``
    hook. Idempotent — re-stamping is a no-op semantically because the
    Nowlez E2 hook only checks ``IS NULL`` before sending; we overwrite the
    timestamp rather than guard so the column always reflects the most
    recent dispatch attempt for observability/debugging.

    Raises ``LookupError`` if the case doesn't exist (mirrors toggle_refresh).
    """
    _assert_valid_cnr(cnr)
    row = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if row is None:
        raise LookupError(
            f"mark_first_ndoh_email_sent: case not found for "
            f"user_id={user_id} cnr={cnr!r}"
        )
    row.first_ndoh_email_sent_at = datetime.now(timezone.utc)
    row.updated_at = datetime.now(timezone.utc)
    s.flush()


def was_first_ndoh_email_sent(s: Session, *, user_id: uuid.UUID, cnr: str) -> bool:
    """Return whether the first-NDOH email has already been sent for this case.

    B.5b: Postgres replacement for the SQLite ``was_first_ndoh_email_sent``
    hook. Unlike ``toggle_refresh`` / ``mark_first_ndoh_email_sent`` this is
    a read-side check used to *gate* the send, so a missing case row returns
    ``False`` (no row → no prior email → caller is free to dispatch and then
    upsert). Callers that need a strict not-found signal should call
    ``get_by_cnr`` separately.
    """
    _assert_valid_cnr(cnr)
    row = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if row is None:
        return False
    return row.first_ndoh_email_sent_at is not None


def delete_case(s: Session, *, user_id: uuid.UUID, cnr: str) -> bool:
    """Delete the Case row plus its case_preferences sibling.

    A-10 audit fix: case_preferences has FK only to users (ON DELETE CASCADE
    on users.id), not to cases. Deleting the Case row alone leaves orphan
    preference rows behind. We issue an explicit DELETE on case_preferences
    in the same transaction.

    Order follows audit fix A-R1 (see handlers/monitoring/forget_command.py
    in 0705): delete prefs FIRST, then the Case row. Today both deletes
    are in the same txn so order doesn't change observable behaviour, but
    if a future refactor ever splits these across commits the prefs-first
    order preserves the no-orphan invariant defense-in-depth.
    """
    s.execute(
        delete(CasePreferences).where(
            CasePreferences.user_id == user_id,
            CasePreferences.cnr == cnr,
        )
    )
    row = get_by_cnr(s, user_id=user_id, cnr=cnr)
    if row is None:
        s.flush()
        return False
    s.delete(row)
    s.flush()
    return True


def create_manual_case(
    s: Session,
    *,
    user_id: uuid.UUID,
    forum: str,
    forum_case_ref: str | None = None,
    title: str | None = None,
    court: str | None = None,
    case_number: str | None = None,
    filing_year: int | None = None,
    stage: str | None = None,
    case_status: str | None = None,
    next_hearing_date: datetime | None = None,
    judge: str | None = None,
    client_id: str | None = None,
    notes: str | None = None,
    case_detail_json: dict[str, Any] | None = None,
) -> Case:
    """Create-or-update a manually-entered, non-eCourts case with no fetch.

    Manual cases carry no CNR and are never auto-refreshed (source='manual',
    refresh_enabled=False → excluded by get_due_for_refresh). Identity is
    (user_id, forum, forum_case_ref); when the user has no official number a
    synthetic 'm-<uuid4hex>' ref is minted so the (forum, ref) unique key still
    has teeth. `case_detail_json` should follow the scraper-flat schema the
    calendar/timeline extractor already reads (filing_date / next_hearing_date /
    decision_date / hearing_history[] / interim_orders[] / final_orders[]).
    """
    if forum in _ECOURTS_FORUMS:
        raise ValueError("use upsert_case for eCourts forums")
    ref = forum_case_ref or f"m-{uuid.uuid4().hex}"
    _assert_valid_ref(forum, cnr=None, forum_case_ref=ref)
    detail = case_detail_json or {}
    fields: dict[str, Any] = {
        "title": title,
        "court": court,
        "case_number": case_number or ref,
        "filing_year": filing_year,
        "stage": stage,
        "case_status": case_status,
        "next_hearing_date": next_hearing_date,
        "judge": judge,
        "notes": notes,
        "case_detail_json": detail,
        "parties": detail.get("parties", []),
        "history": detail.get("hearing_history", []),
        "updated_at": datetime.now(timezone.utc),
    }
    existing = get_by_ref(s, user_id=user_id, forum=forum, forum_case_ref=ref)
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        s.flush()
        return existing
    row = Case(
        user_id=user_id,
        cnr=None,
        portal=None,
        forum=forum,
        forum_case_ref=ref,
        source="manual",
        refresh_enabled=False,
        client_id=client_id,
        raw_response={},
        **fields,
    )
    s.add(row)
    s.flush()
    return row


def update_fields_by_ref(
    s: Session, *, user_id: uuid.UUID, forum: str, forum_case_ref: str, **cols: Any,
) -> list[str]:
    """Partial patch keyed on (user_id, forum, forum_case_ref) — no CNR assertion.

    Sibling of update_fields for non-eCourts / manual cases. Same allow-list and
    last_change_at bump semantics. Unknown column -> ValueError; missing row -> [].
    """
    unknown = set(cols) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_fields_by_ref: unknown column(s) {sorted(unknown)}")
    row = get_by_ref(s, user_id=user_id, forum=forum, forum_case_ref=forum_case_ref)
    if row is None:
        return []
    changed: list[str] = []
    for k, v in cols.items():
        if getattr(row, k) != v:
            setattr(row, k, v)
            changed.append(k)
    if any(c in _UPDATABLE_DIFFABLE for c in changed):
        row.last_change_at = datetime.now(timezone.utc)
    if changed:
        row.updated_at = datetime.now(timezone.utc)
    s.flush()
    return changed


def delete_case_by_ref(
    s: Session, *, user_id: uuid.UUID, forum: str, forum_case_ref: str,
) -> bool:
    """Delete a non-eCourts / manual Case row by its generic identity.

    Manual cases have a NULL cnr, so delete_case (cnr-keyed) can't reach them.
    CasePreferences is still cnr-keyed (generalized in Phase 1E); manual cases
    have no prefs rows, so there is nothing extra to clean up here yet.
    """
    row = get_by_ref(s, user_id=user_id, forum=forum, forum_case_ref=forum_case_ref)
    if row is None:
        return False
    s.delete(row)
    s.flush()
    return True


def _case_to_row_payload(c: DataCase) -> dict[str, Any]:
    portal = classify_cnr(c.cnr)
    return {
        "title": c.title,
        "court": c.court,
        "stage": c.stage,
        "case_status": c.stage,
        "next_hearing_date": _to_dt(c.next_hearing_date),
        "judge": c.judge,
        "portal": portal,
        # Multi-forum: eCourts writes are always an eCourts forum, auto source,
        # with forum_case_ref == cnr (upsert_case overrides ref with its cnr param).
        "forum": "ecourts_highcourt" if portal == "highcourt" else "ecourts_district",
        "source": "ecourts_auto",
        "forum_case_ref": c.cnr,
        "parties": [asdict(p) for p in c.parties],
        "acts": [asdict(a) for a in c.acts],
        "history": [_dataclass_with_dates(h) for h in c.history],
        "fir": _dataclass_or_none(c.fir),
        "objections": _dataclass_or_none(c.objections),
        "category": _dataclass_or_none(c.category),
        "raw_response": _dataclass_with_dates(c),
    }


def _to_dt(d: date | None) -> datetime | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _dataclass_or_none(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    return _dataclass_with_dates(obj)


def _dataclass_with_dates(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_dataclass_with_dates(x) for x in obj]
    if is_dataclass(obj):
        d = asdict(obj)
        return {k: _serialize_value(v) for k, v in d.items()}
    return _serialize_value(obj)


def _serialize_value(v: Any) -> Any:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, list):
        return [_serialize_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _serialize_value(val) for k, val in v.items()}
    return v
