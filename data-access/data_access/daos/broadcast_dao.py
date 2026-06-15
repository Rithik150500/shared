"""DAOs for the broadcast ledger (``wa_broadcast_log``) and suppression
list (``wa_suppression``).

Uses the same dialect-aware INSERT ON CONFLICT DO NOTHING idiom as
``whatsapp_dao.claim_message`` and ``whatsapp_delivery.dispatch.worker._claim_daily_send_slot``.
Both Postgres (production) and SQLite (unit tests) are supported via the
``pg_insert`` / ``sqlite_insert`` branch on ``session.get_bind().dialect.name``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models.broadcast import WaBroadcastLog, WaSuppression


# ---------------------------------------------------------------------------
# Suppression helpers
# ---------------------------------------------------------------------------


def suppress(
    session: Session,
    *,
    wa_digits: str,
    reason: str,
    source: str | None = None,
) -> None:
    """Add a phone number to the suppression deny list.

    Idempotent: if the number is already present the INSERT is silently
    ignored (ON CONFLICT DO NOTHING on ``wa_digits``). The first ``reason``
    written wins.
    """
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(WaSuppression)
        .values(wa_digits=wa_digits, reason=reason, source=source)
        .on_conflict_do_nothing(index_elements=["wa_digits"])
    )
    session.execute(stmt)
    session.flush()


def is_suppressed(session: Session, wa_digits: str) -> bool:
    """Return True if the phone number is in the suppression list."""
    row = session.execute(
        select(WaSuppression.id).where(WaSuppression.wa_digits == wa_digits)
    ).first()
    return row is not None


def load_suppressed_set(session: Session) -> set[str]:
    """Return the full set of suppressed ``wa_digits`` (for bulk pre-filter)."""
    rows = session.execute(select(WaSuppression.wa_digits)).scalars().all()
    return set(rows)


# ---------------------------------------------------------------------------
# Broadcast ledger helpers
# ---------------------------------------------------------------------------


def claim_send(
    session: Session,
    *,
    campaign: str,
    wa_digits: str,
    tier: str | None,
    template_name: str,
    language: str,
) -> bool:
    """Claim a slot in the broadcast ledger for ``(campaign, wa_digits)``.

    Returns:
      True  — row was newly inserted; caller should proceed with the send.
      False — row already existed (retry / resume); caller should skip.

    Uses INSERT ON CONFLICT DO NOTHING on the UNIQUE constraint
    ``wa_broadcast_log_campaign_phone_unique`` so concurrent workers are safe.
    """
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(WaBroadcastLog)
        .values(
            campaign=campaign,
            wa_digits=wa_digits,
            tier=tier,
            template_name=template_name,
            language=language,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["campaign", "wa_digits"])
    )
    result = session.execute(stmt)
    session.flush()
    return result.rowcount > 0


def mark_sent(
    session: Session,
    *,
    campaign: str,
    wa_digits: str,
    wamid: str,
) -> None:
    """Record that the Meta Cloud API accepted the send.

    Sets ``status='sent'``, ``meta_message_id=wamid``, and ``sent_at=now()``.
    """
    session.execute(
        update(WaBroadcastLog)
        .where(
            WaBroadcastLog.campaign == campaign,
            WaBroadcastLog.wa_digits == wa_digits,
        )
        .values(
            status="sent",
            meta_message_id=wamid,
            sent_at=datetime.now(timezone.utc),
        )
    )
    session.flush()


def mark_failed_local(
    session: Session,
    *,
    campaign: str,
    wa_digits: str,
    error_code: int | None = None,
    reason: str | None = None,
) -> None:
    """Record a local (pre-Meta) failure for the given recipient.

    Sets ``status='failed'``, optional ``error_code``, optional
    ``failure_reason``, and ``failed_at=now()``.
    """
    session.execute(
        update(WaBroadcastLog)
        .where(
            WaBroadcastLog.campaign == campaign,
            WaBroadcastLog.wa_digits == wa_digits,
        )
        .values(
            status="failed",
            error_code=error_code,
            failure_reason=reason,
            failed_at=datetime.now(timezone.utc),
        )
    )
    session.flush()


def apply_broadcast_status(
    session: Session,
    *,
    wamid: str,
    status: str,
    error_code: int | None = None,
    failure_reason: str | None = None,
) -> int:
    """Apply an inbound Meta webhook status receipt to the broadcast log row.

    Looks up the row by ``meta_message_id`` (wamid) and updates:
    - ``status``
    - the matching timestamp column (``sent_at``, ``delivered_at``,
      ``read_at``, or ``failed_at``)
    - ``error_code`` and ``failure_reason`` when provided

    Returns the number of rows updated (0 if the wamid is unknown).
    """
    ts_col = {
        "sent": "sent_at",
        "delivered": "delivered_at",
        "read": "read_at",
        "failed": "failed_at",
    }
    values: dict = {"status": status}
    col = ts_col.get(status)
    if col:
        values[col] = datetime.now(timezone.utc)
    if error_code is not None:
        values["error_code"] = error_code
    if failure_reason is not None:
        values["failure_reason"] = failure_reason

    result = session.execute(
        update(WaBroadcastLog)
        .where(WaBroadcastLog.meta_message_id == wamid)
        .values(**values)
    )
    session.flush()
    return result.rowcount


def get_by_wamid(
    session: Session, wamid: str
) -> Optional[WaBroadcastLog]:
    """Return the broadcast log row matching ``meta_message_id``, or None."""
    return session.execute(
        select(WaBroadcastLog).where(WaBroadcastLog.meta_message_id == wamid)
    ).scalar_one_or_none()


def already_done_set(session: Session, campaign: str) -> set[str]:
    """Return the set of ``wa_digits`` already in the ledger for ``campaign``.

    Used by the resume/retry loop to skip recipients that were already
    claimed in a previous run (regardless of their current status).
    """
    rows = session.execute(
        select(WaBroadcastLog.wa_digits).where(
            WaBroadcastLog.campaign == campaign
        )
    ).scalars().all()
    return set(rows)


def sent_count_since(session: Session, campaign: str, *, hours: int) -> int:
    """Count broadcast rows for ``campaign`` with ``sent_at`` within the last
    ``hours`` hours.

    Used by the driver's daily-cap check. The cutoff is computed in Python
    (``datetime.now(timezone.utc) - timedelta(hours=hours)``) for DB
    portability — no dialect-specific NOW() arithmetic needed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = session.execute(
        select(func.count()).where(
            WaBroadcastLog.campaign == campaign,
            WaBroadcastLog.sent_at >= cutoff,
        )
    ).scalar_one()
    return count
