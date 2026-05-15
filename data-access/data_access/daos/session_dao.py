from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import AuthSession


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(
    session: Session,
    *,
    user_id: uuid.UUID,
    refresh_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
    ttl_days: int = 30,
) -> AuthSession:
    now = datetime.now(timezone.utc)
    s = AuthSession(
        user_id=user_id,
        refresh_token_hash=_hash(refresh_token),
        created_at=now,
        expires_at=now + timedelta(days=ttl_days),
        last_used_at=func.now(),
        user_agent=user_agent,
        ip_address=ip_address,
    )
    session.add(s)
    session.flush()
    session.refresh(s)
    return s


def lookup_by_token(session: Session, refresh_token: str) -> AuthSession | None:
    h = _hash(refresh_token)
    stmt = select(AuthSession).where(
        AuthSession.refresh_token_hash == h,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > func.now(),
    )
    return session.execute(stmt).scalar_one_or_none()


def touch_last_used(session: Session, session_id: uuid.UUID) -> None:
    session.execute(
        update(AuthSession).where(AuthSession.id == session_id).values(last_used_at=func.now())
    )
    session.flush()


def revoke_by_token(session: Session, refresh_token: str) -> None:
    h = _hash(refresh_token)
    session.execute(
        update(AuthSession)
        .where(AuthSession.refresh_token_hash == h, AuthSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    session.flush()


def revoke_all_except(session: Session, user_id: uuid.UUID, *, except_session_id: uuid.UUID) -> None:
    session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.id != except_session_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    session.flush()


def cleanup_expired(session: Session, older_than_days: int = 7) -> int:
    """Hard-delete sessions expired more than older_than_days ago. Returns rowcount."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = session.execute(delete(AuthSession).where(AuthSession.expires_at < cutoff))
    session.flush()
    return result.rowcount or 0
