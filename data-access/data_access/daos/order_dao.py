"""DAO for case_orders + case_orders_nowlez."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from data_access.models.case import Case, CaseOrder, CaseOrderNowlez
from ecourts_client.models import OrderRef


def ensure_munshi_order(s: Session, *, case_id: uuid.UUID, order_ref: OrderRef) -> CaseOrder:
    """Insert/update case_orders only; never touches case_orders_nowlez."""
    return _upsert_case_order(s, case_id=case_id, order_ref=order_ref)


def ensure_nowlez_order(s: Session, *, case_id: uuid.UUID, order_ref: OrderRef) -> CaseOrder:
    """Insert/update case_orders AND create a default case_orders_nowlez extension row."""
    order = _upsert_case_order(s, case_id=case_id, order_ref=order_ref)
    existing = get_nowlez_extension(s, order_id=order.id)
    if existing is None:
        ext = CaseOrderNowlez(order_id=order.id)
        s.add(ext)
        s.flush()
    return order


def _upsert_case_order(s: Session, *, case_id: uuid.UUID, order_ref: OrderRef) -> CaseOrder:
    stmt = select(CaseOrder).where(
        CaseOrder.case_id == case_id, CaseOrder.order_id == order_ref.order_id,
    )
    row = s.execute(stmt).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = CaseOrder(
            case_id=case_id,
            order_id=order_ref.order_id,
            order_date=order_ref.order_date,
            order_url=order_ref.order_url,
            url_fetched_at=now,
            updated_at=now,
        )
        s.add(row)
    else:
        row.order_date = order_ref.order_date
        if order_ref.order_url:
            row.order_url = order_ref.order_url
            row.url_fetched_at = now
        row.updated_at = now
    s.flush()
    return row


def upsert_nowlez_extension(
    s: Session,
    *,
    order_id: uuid.UUID,
    file_path: str | None = None,
    file_storage: str | None = None,
    page_count: int | None = None,
    file_size_bytes: int | None = None,
    preprocessed: bool | None = None,
    preprocessed_markdown_path: str | None = None,
    preprocessed_at: datetime | None = None,
    retry_count: int | None = None,
    last_retry_at: datetime | None = None,
    permanently_failed: bool | None = None,
    permanent_failure_reason: str | None = None,
    uploaded_at: datetime | None = None,
) -> CaseOrderNowlez:
    ext = get_nowlez_extension(s, order_id=order_id)
    if ext is None:
        ext = CaseOrderNowlez(order_id=order_id)
        s.add(ext)
    for field, value in [
        ("file_path", file_path),
        ("file_storage", file_storage),
        ("page_count", page_count),
        ("file_size_bytes", file_size_bytes),
        ("preprocessed", preprocessed),
        ("preprocessed_markdown_path", preprocessed_markdown_path),
        ("preprocessed_at", preprocessed_at),
        ("retry_count", retry_count),
        ("last_retry_at", last_retry_at),
        ("permanently_failed", permanently_failed),
        ("permanent_failure_reason", permanent_failure_reason),
        ("uploaded_at", uploaded_at),
    ]:
        if value is not None:
            setattr(ext, field, value)
    s.flush()
    return ext


def get_nowlez_extension(s: Session, *, order_id: uuid.UUID) -> CaseOrderNowlez | None:
    return s.execute(
        select(CaseOrderNowlez).where(CaseOrderNowlez.order_id == order_id)
    ).scalar_one_or_none()


def get_orders_for_case(s: Session, *, case_id: uuid.UUID) -> list[CaseOrder]:
    stmt = (
        select(CaseOrder)
        .where(CaseOrder.case_id == case_id)
        .order_by(CaseOrder.order_date.desc())
    )
    return list(s.execute(stmt).scalars())


def mark_order_user_notified(s: Session, *, order_id: uuid.UUID) -> None:
    """Stamp ``CaseOrderNowlez.user_notified_at`` to now (UTC).

    B.5b: Postgres replacement for the SQLite ``mark_order_user_notified``
    hook (E3 — order delivered notification). Creates the
    ``case_orders_nowlez`` extension row if missing (uses the same upsert
    pattern as ``upsert_nowlez_extension``) so callers don't have to
    pre-create the row for Munshi-only orders that later get notified.

    Idempotent — overwrites the timestamp on each call.
    """
    ext = get_nowlez_extension(s, order_id=order_id)
    if ext is None:
        ext = CaseOrderNowlez(order_id=order_id)
        s.add(ext)
    ext.user_notified_at = datetime.now(timezone.utc)
    s.flush()


def get_unnotified_orders_for_case(
    s: Session, *, case_id: uuid.UUID,
) -> list[CaseOrder]:
    """Return orders for ``case_id`` whose user-notified hook has not fired.

    "Unnotified" means either:
      - there is no ``CaseOrderNowlez`` extension row at all (legacy /
        Munshi-only order not yet promoted), or
      - the extension row exists but ``user_notified_at IS NULL``.

    Ordered newest-first by ``order_date`` (mirrors ``get_orders_for_case``)
    so the Nowlez E3 hook can iterate in user-facing chronological order.

    B.5b: feeds the order-notified hook that runs after fetch_orders.
    """
    stmt = (
        select(CaseOrder)
        .outerjoin(CaseOrderNowlez, CaseOrderNowlez.order_id == CaseOrder.id)
        .where(
            CaseOrder.case_id == case_id,
            or_(
                CaseOrderNowlez.order_id.is_(None),
                CaseOrderNowlez.user_notified_at.is_(None),
            ),
        )
        .order_by(CaseOrder.order_date.desc())
    )
    return list(s.execute(stmt).scalars())


def get_failed_orders(
    s: Session,
    *,
    user_id: uuid.UUID | None = None,
    max_retries: int = 3,
    retry_cooldown: timedelta = timedelta(hours=1),
) -> list[tuple[CaseOrder, CaseOrderNowlez]]:
    """Return failed-but-eligible-for-retry orders + their nowlez extension.

    Mirrors the SQLite-side ``get_failed_orders`` predicate
    (``preprocessed = False AND permanently_failed = False AND
    retry_count < max_retries``) and *extends* it with a per-row retry
    cooldown: rows whose ``last_retry_at`` is within ``retry_cooldown`` of
    "now" are filtered out so the worker doesn't hot-loop a single failing
    order. Rows that have never been retried (``last_retry_at IS NULL``)
    pass the cooldown filter — first retry is always allowed.

    Args:
        user_id: if set, scope to orders belonging to that user via the
            ``case.user_id`` FK. If None, return failed orders across all
            users (used by the global retry worker).
        max_retries: orders with ``retry_count >= max_retries`` are excluded.
            Defaults to 3 (matches the legacy SQLite default — Nowlez side
            is free to pass the configured value if it diverges).
        retry_cooldown: skip rows whose ``last_retry_at`` is more recent
            than this. Defaults to 1 hour.

    Ordered by ``CaseOrder.created_at ASC`` (oldest first) — matches the
    SQLite version so the retry worker drains the backlog in FIFO order.

    B.5b: replaces the SQLite-side failed-retry pipeline (legacy
    ``backend/db/cases.py::get_failed_orders``).
    """
    cutoff = datetime.now(timezone.utc) - retry_cooldown
    cooldown_clause = or_(
        CaseOrderNowlez.last_retry_at.is_(None),
        CaseOrderNowlez.last_retry_at < cutoff,
    )
    stmt = (
        select(CaseOrder, CaseOrderNowlez)
        .join(CaseOrderNowlez, CaseOrderNowlez.order_id == CaseOrder.id)
        .where(
            CaseOrderNowlez.preprocessed.is_(False),
            CaseOrderNowlez.permanently_failed.is_(False),
            CaseOrderNowlez.retry_count < max_retries,
            cooldown_clause,
        )
        .order_by(CaseOrder.created_at.asc())
    )
    if user_id is not None:
        stmt = stmt.join(Case, Case.id == CaseOrder.case_id).where(
            Case.user_id == user_id,
        )
    return [(co, ext) for co, ext in s.execute(stmt).all()]


def increment_order_retry_count(
    s: Session, *, order_id: uuid.UUID, max_retries: int = 3,
) -> int:
    """Increment ``CaseOrderNowlez.retry_count`` and return the new count.

    Also stamps ``last_retry_at = now()`` so the retry-cooldown clause in
    ``get_failed_orders`` deselects the row until the cooldown expires.
    When ``retry_count`` reaches ``max_retries`` we also flip
    ``permanently_failed = True`` so the row drops out of the retry
    queue entirely (mirrors the legacy SQLite finalisation step).

    The increment is "atomic" only in the sense that it runs inside the
    caller's open transaction — concurrent workers must rely on advisory
    row-locking (``SELECT FOR UPDATE`` / app-level claim) rather than this
    DAO method for serialization. Today the Nowlez retry worker is single-
    instance per worker pod, so this is sufficient.

    Raises ``LookupError`` if the extension row doesn't exist. Callers
    should call ``upsert_nowlez_extension`` first if the row is unknown.

    Returns the post-increment ``retry_count`` for logging / decision-making.
    """
    ext = get_nowlez_extension(s, order_id=order_id)
    if ext is None:
        raise LookupError(
            f"increment_order_retry_count: no case_orders_nowlez row for "
            f"order_id={order_id}"
        )
    ext.retry_count = (ext.retry_count or 0) + 1
    ext.last_retry_at = datetime.now(timezone.utc)
    if ext.retry_count >= max_retries:
        ext.permanently_failed = True
    s.flush()
    return ext.retry_count


def get_legacy_orders_by_case(s: Session, legacy_case_id: int) -> list[dict[str, Any]]:
    """Used only by the migration script; reads from `_legacy_nowlez_case_orders`.

    The schema column is named `client_case_id` (historical artefact of the
    legacy SQLite schema). The Python parameter is named `legacy_case_id`
    for clarity; the mapping happens here. See the canonical-schema block
    in `migrations/RUNBOOK_refetch_nowlez_cases.md` §0.1.
    """
    from sqlalchemy import text
    rows = s.execute(
        text("SELECT * FROM _legacy_nowlez_case_orders WHERE client_case_id = :cid"),
        {"cid": legacy_case_id},
    ).mappings().all()
    return [dict(r) for r in rows]
