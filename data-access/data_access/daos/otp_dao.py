from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import OtpCode


def insert(
    session: Session,
    *,
    phone: str,
    code_hash: str,
    channel: str,
    ttl_minutes: int = 10,
    ip_address: str | None = None,
) -> OtpCode:
    now = datetime.now(timezone.utc)
    o = OtpCode(
        phone=phone,
        code_hash=code_hash,
        channel=channel,
        expires_at=now + timedelta(minutes=ttl_minutes),
        ip_address=ip_address,
    )
    session.add(o)
    session.flush()
    return o


def get_by_id(session: Session, otp_id: uuid.UUID) -> OtpCode | None:
    return session.get(OtpCode, otp_id)


def get_active(session: Session, phone: str) -> OtpCode | None:
    """Most recent unused, unexpired OTP for the phone."""
    stmt = (
        select(OtpCode)
        .where(
            OtpCode.phone == phone,
            OtpCode.used_at.is_(None),
            OtpCode.expires_at > func.now(),
        )
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def mark_delivered(session: Session, otp_id: uuid.UUID, *, provider_id: str) -> None:
    o = session.get(OtpCode, otp_id)
    if o is None:
        return
    o.delivery_status = "delivered"
    o.delivery_provider_id = provider_id
    session.flush()


def mark_failed(session: Session, otp_id: uuid.UUID) -> None:
    o = session.get(OtpCode, otp_id)
    if o is None:
        return
    o.delivery_status = "failed"
    session.flush()


def mark_used(session: Session, otp_id: uuid.UUID) -> None:
    o = session.get(OtpCode, otp_id)
    if o is None:
        return
    o.used_at = datetime.now(timezone.utc)
    session.flush()


def decrement_attempts(session: Session, otp_id: uuid.UUID) -> int:
    """Decrement attempts_remaining and return the new value. Floors at 0."""
    o = session.get(OtpCode, otp_id)
    if o is None:
        return 0
    o.attempts_remaining = max(0, o.attempts_remaining - 1)
    session.flush()
    return o.attempts_remaining


def count_within(session: Session, phone: str, minutes: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    stmt = (
        select(func.count())
        .select_from(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.created_at > cutoff)
    )
    return session.execute(stmt).scalar_one()


def count_by_ip_within(session: Session, ip_address: str, minutes: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    stmt = (
        select(func.count())
        .select_from(OtpCode)
        .where(OtpCode.ip_address == ip_address, OtpCode.created_at > cutoff)
    )
    return session.execute(stmt).scalar_one()


def cleanup_expired(session: Session, older_than_hours: int = 24) -> int:
    """Hard-delete OTPs expired more than older_than_hours ago. Returns rowcount."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    result = session.execute(delete(OtpCode).where(OtpCode.expires_at < cutoff))
    session.flush()
    return result.rowcount or 0
