"""DAO for case_orders + case_orders_nowlez."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_access.models.case import CaseOrder, CaseOrderNowlez
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


def get_legacy_orders_by_case(s: Session, legacy_case_id: int) -> list[dict[str, Any]]:
    """Used only by the migration script; reads from `_legacy_nowlez_case_orders`."""
    from sqlalchemy import text
    rows = s.execute(
        text("SELECT * FROM _legacy_nowlez_case_orders WHERE client_case_id = :cid"),
        {"cid": legacy_case_id},
    ).mappings().all()
    return [dict(r) for r in rows]
