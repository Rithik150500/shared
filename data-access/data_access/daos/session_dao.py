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
    family_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> AuthSession:
    """Persist a refresh-token session row.

    ``family_id`` defaults to a fresh uuid (a new login = a new rotation family);
    the rotation path passes the parent's family_id to keep the lineage. When
    ``expires_at`` is given (rotation) it is used verbatim so a rotated token
    inherits the family's ABSOLUTE expiry; otherwise it is ttl_days from now.
    """
    now = datetime.now(timezone.utc)
    s = AuthSession(
        user_id=user_id,
        refresh_token_hash=_hash(refresh_token),
        family_id=family_id or uuid.uuid4(),
        created_at=now,
        expires_at=expires_at if expires_at is not None else now + timedelta(days=ttl_days),
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


def get_by_token_any_state(session: Session, refresh_token: str) -> AuthSession | None:
    """Look up a session by token hash REGARDLESS of revoked/expired state.

    Rotation + reuse-detection need to see a revoked row (a replayed rotated
    token) that lookup_by_token would hide. The hash is unique so this returns
    at most one row."""
    h = _hash(refresh_token)
    return session.execute(
        select(AuthSession).where(AuthSession.refresh_token_hash == h)
    ).scalar_one_or_none()


def revoke_for_rotation(
    session: Session, *, session_id: uuid.UUID, replaced_by: uuid.UUID, now: datetime
) -> int:
    """Atomically claim a rotation: mark the row revoked + point it at its
    successor, ONLY if still live. Returns rowcount (1 = we claimed it; 0 = a
    concurrent refresh already rotated it). Branch on the rowcount — no
    read-then-write TOCTOU window."""
    result = session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now, replaced_by=replaced_by)
    )
    session.flush()
    return result.rowcount or 0


def revoke_family(session: Session, family_id: uuid.UUID, *, now: datetime | None = None) -> int:
    """Revoke every still-active session in a rotation family (reuse-detection
    response). Returns the number of sessions revoked."""
    result = session.execute(
        update(AuthSession)
        .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now or func.now())
    )
    session.flush()
    return result.rowcount or 0


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
