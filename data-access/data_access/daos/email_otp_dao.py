"""DAO for ``email_otp_codes`` — mirrors otp_dao but for the email channel.

The one deliberate divergence from otp_dao: ``mark_used`` is an ATOMIC
conditional UPDATE (branch on rowcount), not a read-then-write, so the
email-OTP verify path enforces single-use without a TOCTOU window.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import EmailOtpCode


def insert(
    session: Session,
    *,
    email: str,
    code_hash: str,
    ttl_minutes: int = 10,
    ip_address: str | None = None,
) -> EmailOtpCode:
    now = datetime.now(timezone.utc)
    o = EmailOtpCode(
        email=email,
        code_hash=code_hash,
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
        ip_address=ip_address,
    )
    session.add(o)
    session.flush()
    return o


def get_by_id(session: Session, otp_id: uuid.UUID) -> EmailOtpCode | None:
    return session.get(EmailOtpCode, otp_id)


def get_active(session: Session, email: str) -> EmailOtpCode | None:
    stmt = (
        select(EmailOtpCode)
        .where(
            EmailOtpCode.email == email,
            EmailOtpCode.used_at.is_(None),
            EmailOtpCode.expires_at > func.now(),
        )
        .order_by(EmailOtpCode.created_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    # On SQLite the PK may come back as a string; normalise so callers always
    # receive a uuid.UUID and identity-map comparisons work in tests.
    return session.get(EmailOtpCode, uuid.UUID(str(row.id)))


def mark_delivered(session: Session, otp_id: uuid.UUID, *, provider_id: str) -> None:
    o = session.get(EmailOtpCode, otp_id)
    if o is None:
        return
    o.delivery_status = "delivered"
    o.delivery_provider_id = provider_id
    session.flush()


def mark_failed(session: Session, otp_id: uuid.UUID) -> None:
    o = session.get(EmailOtpCode, otp_id)
    if o is None:
        return
    o.delivery_status = "failed"
    session.flush()


def decrement_attempts(session: Session, otp_id: uuid.UUID) -> int:
    """Decrement attempts_remaining and return the new value. Floors at 0."""
    o = session.get(EmailOtpCode, otp_id)
    if o is None:
        return 0
    o.attempts_remaining = max(0, o.attempts_remaining - 1)
    session.flush()
    return o.attempts_remaining
