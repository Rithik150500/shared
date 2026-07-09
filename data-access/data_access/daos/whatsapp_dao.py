"""DAOs for WhatsApp idempotency and delivery logging.

Shared by both brands so cross-brand inbound dedup + per-send tracking live
in one shared DB. ``claim_message`` is dialect-aware (Postgres + SQLite)
because the SQLite test path can't use Postgres-only ON CONFLICT syntax.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import User, UserNowlez
from ..models.whatsapp import MessageLog, WhatsAppDeliveryLog


def claim_message(session: Session, *, meta_message_id: str, user_phone: str) -> bool:
    """Return True on first sighting; False on Meta retry of same id.

    Uses INSERT ON CONFLICT DO NOTHING on (meta_message_id) so concurrent
    workers can race safely. ``session.commit()`` is on the caller's behalf —
    the row must be durable before any downstream side-effect.
    """
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(MessageLog)
        .values(meta_message_id=meta_message_id, user_phone=user_phone)
        .on_conflict_do_nothing(index_elements=["meta_message_id"])
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount > 0


def log_send_enqueued(
    session: Session,
    *,
    user_id: uuid.UUID,
    template_name: str,
    brand: str,
    rq_job_id: str,
    related_case_id: uuid.UUID | None = None,
    related_order_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record a freshly-enqueued send. Returns the row id for later updates."""
    row = WhatsAppDeliveryLog(
        user_id=user_id,
        template_name=template_name,
        brand=brand,
        rq_job_id=rq_job_id,
        related_case_id=related_case_id,
        related_order_id=related_order_id,
    )
    session.add(row)
    session.flush()
    session.commit()
    return row.id


def set_meta_message_id(
    session: Session,
    *,
    rq_job_id: str | None,
    meta_message_id: str,
    user_id: uuid.UUID | str | None = None,
    template_name: str | None = None,
    brand: str | None = None,
    send_date_ist=None,
) -> int:
    """Link Meta's wamid to a delivery-log row so status webhooks can match it.

    The wamid MUST land on a row (status receipts match on ``meta_message_id``),
    but binding by ``rq_job_id`` alone is unreliable in the live two-worker
    topology (embedded app worker + dedicated worker) — the row created by
    ``_claim_daily_send_slot`` is keyed by ``(user_id, template_name,
    send_date_ist)``, not ``rq_job_id`` in a way the bind reliably resolves. So
    resolve the row deterministically:

      1. by ``rq_job_id`` (producer-side ``log_send_enqueued`` rows), else
      2. by the daily-claim natural key ``(user_id, template_name,
         send_date_ist)``, else
      3. if no row exists (transactional sends that never logged), INSERT one.

    Only ever fills a NULL ``meta_message_id`` (never overwrites).
    """
    if rq_job_id:
        result = session.execute(
            update(WhatsAppDeliveryLog)
            .where(
                WhatsAppDeliveryLog.rq_job_id == rq_job_id,
                WhatsAppDeliveryLog.meta_message_id.is_(None),
            )
            .values(meta_message_id=meta_message_id)
        )
        if result.rowcount:
            session.commit()
            return result.rowcount

    if user_id and template_name and send_date_ist is not None:
        result = session.execute(
            update(WhatsAppDeliveryLog)
            .where(
                WhatsAppDeliveryLog.user_id == user_id,
                WhatsAppDeliveryLog.template_name == template_name,
                WhatsAppDeliveryLog.send_date_ist == send_date_ist,
                WhatsAppDeliveryLog.meta_message_id.is_(None),
            )
            .values(meta_message_id=meta_message_id)
        )
        if result.rowcount:
            session.commit()
            return result.rowcount

    if user_id and template_name and brand:
        session.add(
            WhatsAppDeliveryLog(
                user_id=user_id,
                template_name=template_name,
                brand=brand,
                rq_job_id=rq_job_id,
                meta_message_id=meta_message_id,
                delivery_status="sent",
            )
        )
        session.commit()
        return 1

    session.commit()
    return 0


def update_delivery_status(
    session: Session,
    *,
    meta_message_id: str,
    status: str,
    timestamp: int,
    failure_reason: str | None = None,
) -> int:
    """Apply a Meta status receipt (sent / delivered / read / failed)."""
    ts = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    col_map = {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}
    values: dict = {"delivery_status": status}
    if failure_reason is not None:
        values["failure_reason"] = failure_reason
    if status in col_map:
        values[col_map[status]] = ts
    result = session.execute(
        update(WhatsAppDeliveryLog)
        .where(WhatsAppDeliveryLog.meta_message_id == meta_message_id)
        .values(**values)
    )
    session.commit()
    return result.rowcount


def get_users_with_whatsapp_events_enabled(session: Session) -> Iterable[User]:
    """Active users with both a phone number and the events flag on."""
    return session.execute(
        select(User)
        .join(UserNowlez, UserNowlez.user_id == User.id)
        .where(UserNowlez.whatsapp_events_enabled.is_(True))
        .where(User.phone.isnot(None))
        .where(User.is_active.is_(True))
    ).scalars()


def get_users_with_whatsapp_reminders_enabled(session: Session) -> Iterable[User]:
    """Active users with both a phone number and the reminders flag on."""
    return session.execute(
        select(User)
        .join(UserNowlez, UserNowlez.user_id == User.id)
        .where(UserNowlez.whatsapp_reminders_enabled.is_(True))
        .where(User.phone.isnot(None))
        .where(User.is_active.is_(True))
    ).scalars()


def update_nowlez_preferences(
    session: Session,
    *,
    user_id: uuid.UUID,
    whatsapp_events_enabled: bool | None = None,
    whatsapp_reminders_enabled: bool | None = None,
) -> int:
    """Flip the per-user WhatsApp consent flags on users_nowlez.

    Both args are optional — pass only what you want to change. STOP-keyword
    handler passes both as False; the Nowlez settings UI toggles them one at
    a time.
    """
    values: dict = {}
    if whatsapp_events_enabled is not None:
        values["whatsapp_events_enabled"] = whatsapp_events_enabled
    if whatsapp_reminders_enabled is not None:
        values["whatsapp_reminders_enabled"] = whatsapp_reminders_enabled
    if not values:
        return 0
    result = session.execute(
        update(UserNowlez)
        .where(UserNowlez.user_id == user_id)
        .values(**values)
    )
    session.commit()
    return result.rowcount


def count_messages_since(session: Session, since: datetime) -> int:
    return session.execute(
        select(func.count()).select_from(MessageLog).where(MessageLog.received_at >= since)
    ).scalar_one()


def list_deliveries_for_user(session: Session, *, user_id: uuid.UUID, limit: int = 50):
    """Outbound template sends for a user, newest first (whatsapp_delivery_log)."""
    stmt = (select(WhatsAppDeliveryLog)
            .where(WhatsAppDeliveryLog.user_id == user_id)
            .order_by(WhatsAppDeliveryLog.enqueued_at.desc())
            .limit(limit))
    return list(session.execute(stmt).scalars().all())


def list_inbound_for_user(session: Session, *, user_id: uuid.UUID, limit: int = 50):
    """Inbound message receipts (metadata only) for a user, newest first (message_log)."""
    stmt = (select(MessageLog)
            .where(MessageLog.user_id == user_id)
            .order_by(MessageLog.received_at.desc())
            .limit(limit))
    return list(session.execute(stmt).scalars().all())
