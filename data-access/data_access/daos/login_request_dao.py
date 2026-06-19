"""DAO for the unified-auth nonce table ``login_requests``.

Every state transition (confirm pending->confirmed, consume confirmed->consumed)
is a SINGLE atomic conditional UPDATE that branches ONLY on rowcount — never on
a prior SELECT (the load-bearing single-use invariant, §10 of the spec). The
WHERE-clause expiry gate uses func.now() (DB clock authority); ``expires_at`` is
set Python-side at create time, mirroring otp_dao/session_dao.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import LoginRequest


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_web2bot(
    session: Session,
    *,
    token_hash: str,
    brand: str,
    poll_bind_hash: str,
    ttl_seconds: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> LoginRequest:
    now = datetime.now(timezone.utc)
    lr = LoginRequest(
        token_hash=token_hash,
        direction="web2bot",
        status="pending",
        brand=brand,
        user_id=None,
        poll_bind_hash=poll_bind_hash,
        expires_at=now + timedelta(seconds=ttl_seconds),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(lr)
    session.flush()
    return lr


def create_bot2web(
    session: Session,
    *,
    token_hash: str,
    brand: str,
    user_id: uuid.UUID,
    phone: str,
    ttl_seconds: int,
) -> LoginRequest:
    now = datetime.now(timezone.utc)
    lr = LoginRequest(
        token_hash=token_hash,
        direction="bot2web",
        status="pending",
        brand=brand,
        user_id=user_id,
        phone=phone,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(lr)
    session.flush()
    return lr


def get_active_by_token(
    session: Session,
    *,
    token_hash: str,
    statuses: tuple[str, ...] = ("pending", "confirmed"),
) -> LoginRequest | None:
    stmt = (
        select(LoginRequest)
        .where(
            LoginRequest.token_hash == token_hash,
            LoginRequest.expires_at > func.now(),
            LoginRequest.status.in_(statuses),
        )
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_by_id(session: Session, login_id: uuid.UUID) -> LoginRequest | None:
    return session.get(LoginRequest, login_id)


def confirm(
    session: Session,
    *,
    token_hash: str,
    user_id: uuid.UUID,
    phone: str,
) -> int:
    """Atomic pending->confirmed flip. Returns rowcount (1 = this call flipped
    it; 0 = unknown / already-confirmed / expired). Caller branches ONLY on the
    return value."""
    result = session.execute(
        update(LoginRequest)
        .where(
            LoginRequest.token_hash == token_hash,
            LoginRequest.status == "pending",
            LoginRequest.expires_at > func.now(),
        )
        .values(status="confirmed", user_id=user_id, phone=phone)
    )
    session.flush()
    return result.rowcount
