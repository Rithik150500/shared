"""DAO for the unified cases table."""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
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


def count_active_cases_for_user(s: Session, *, user_id: uuid.UUID) -> int:
    """Return the number of refresh-enabled cases the user owns in the shared
    cases table.

    This is the single source of truth for the per-user "active case" count
    behind BOTH surfaces' tier caps (the casepilot web tier limit + the bot's
    Munshi/free cap), so the two agree on one UNIFIED number rather than each
    counting its own store. ``refresh_enabled IS TRUE`` is the canonical
    "still tracked" predicate: cnr-not-found tombstones and manually paused /
    manual rows drop out — matching the bot's long-standing ``_count_active_cases``.
    """
    return int(
        s.execute(
            select(func.count(Case.id))
            .where(Case.user_id == user_id)
            .where(Case.refresh_enabled.is_(True))
        ).scalar_one()
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
    """Cases needing refresh, ordered NULLS FIRST then oldest first."""
    stmt = (
        select(Case)
        .where(Case.refresh_enabled.is_(True))
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


def _case_to_row_payload(c: DataCase) -> dict[str, Any]:
    return {
        "title": c.title,
        "court": c.court,
        "stage": c.stage,
        "case_status": c.stage,
        "next_hearing_date": _to_dt(c.next_hearing_date),
        "judge": c.judge,
        "portal": classify_cnr(c.cnr),
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
