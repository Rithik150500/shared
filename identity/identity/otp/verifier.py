"""Verify a submitted OTP code against the stored argon2id hash."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2 import exceptions as a2err
from sqlalchemy.orm import Session

from data_access.daos import otp_dao

from ..config import settings
from ..errors import OtpAlreadyUsed, OtpAttemptsExhausted, OtpExpired, OtpInvalid

_otp_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST_KB,
    parallelism=settings.ARGON2_PARALLELISM,
)


def verify_otp(session: Session, *, otp_id: uuid.UUID, code: str) -> None:
    """Verify submitted OTP. Raises on invalid / expired / exhausted / unknown; marks used on success.

    State guards run before crypto check so we don't waste argon2id cycles on
    already-revoked / expired OTPs.
    """
    o = otp_dao.get_by_id(session, otp_id)
    if o is None:
        raise OtpInvalid("OTP not found")
    if o.used_at is not None:
        raise OtpAlreadyUsed("OTP has already been used")
    if o.attempts_remaining <= 0:
        raise OtpAttemptsExhausted("OTP attempts exhausted; request a new code")
    if o.expires_at <= datetime.now(timezone.utc):
        raise OtpExpired("OTP has expired; request a new code")

    try:
        _otp_hasher.verify(o.code_hash, code)
    except a2err.VerifyMismatchError:
        otp_dao.decrement_attempts(session, otp_id)
        raise OtpInvalid("Invalid OTP code")
    except a2err.InvalidHash:
        raise OtpInvalid("OTP record corrupted")

    otp_dao.mark_used(session, otp_id)
