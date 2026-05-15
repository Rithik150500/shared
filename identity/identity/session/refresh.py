"""Refresh-token lifecycle wrappers around data_access.session_dao.

issue → opaque token + DB row.
consume → look up by token, touch last_used_at, raise InvalidToken if dead.
revoke → mark revoked_at (for logout / password change).
"""
from __future__ import annotations

import secrets
import uuid

from sqlalchemy.orm import Session

from data_access.daos import session_dao
from data_access.models import AuthSession

from ..config import settings
from ..errors import InvalidToken


def issue_refresh_token(
    session: Session,
    *,
    user_id: uuid.UUID,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, AuthSession]:
    """Generate a URL-safe opaque refresh token, persist its hash, return (raw, AuthSession).

    The raw token must be sent to the client (typically as an HttpOnly cookie)
    and is never persisted. The AuthSession row carries the SHA-256 hash so
    a DB leak doesn't leak active sessions.
    """
    raw = secrets.token_urlsafe(48)  # ~64 chars of base64 entropy
    s = session_dao.create(
        session,
        user_id=user_id,
        refresh_token=raw,
        user_agent=user_agent,
        ip_address=ip_address,
        ttl_days=settings.REFRESH_TTL_DAYS,
    )
    return raw, s


def consume_refresh_token(session: Session, raw_token: str) -> AuthSession:
    """Look up an active session by raw token; touch last_used_at; return it.

    Raises InvalidToken if the token is unknown, revoked, or expired.
    """
    s = session_dao.lookup_by_token(session, raw_token)
    if s is None:
        raise InvalidToken("refresh token invalid, revoked, or expired")
    session_dao.touch_last_used(session, s.id)
    return s


def revoke_refresh_token(session: Session, raw_token: str) -> None:
    """Mark the session as revoked. Idempotent — unknown tokens are no-ops."""
    session_dao.revoke_by_token(session, raw_token)
