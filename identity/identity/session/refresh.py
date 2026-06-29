"""Refresh-token lifecycle wrappers around data_access.session_dao.

issue → opaque token + DB row.
consume → look up by token, touch last_used_at, raise InvalidToken if dead.
revoke → mark revoked_at (for logout / password change).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from data_access.daos import session_dao
from data_access.models import AuthSession

from ..config import settings
from ..errors import InvalidToken, RefreshTokenReuse


def _as_aware(dt: datetime | None) -> datetime | None:
    """Normalise a possibly-naive datetime (SQLite round-trips DateTime as naive)
    to UTC-aware so comparisons/subtraction against datetime.now(timezone.utc)
    don't raise. No-op on already-aware values (Postgres)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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


def rotate_refresh_token(
    session: Session,
    raw_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, AuthSession]:
    """Rotate the presented refresh token: issue a NEW opaque token in the same
    family, revoke the presented one, and return (new_raw, new_session). The
    rotated token inherits the family's ABSOLUTE expiry (no sliding 30-day
    extension).

    Reuse-detection: replaying a token that was already ROTATED (replaced_by set)
    beyond REFRESH_ROTATION_GRACE_SECONDS revokes the whole family and raises
    RefreshTokenReuse. A replay WITHIN the grace window (a concurrent tab/retry)
    instead gets a fresh sibling token so the user is not spuriously logged out.
    An explicitly-revoked token (logout / password change; replaced_by NULL) is a
    plain InvalidToken — no theft alarm.
    """
    now = datetime.now(timezone.utc)
    s = session_dao.get_by_token_any_state(session, raw_token)
    if s is None:
        raise InvalidToken("refresh token invalid")
    if _as_aware(s.expires_at) <= now:
        raise InvalidToken("refresh token expired")

    if s.revoked_at is None:
        # Live token -> normal rotation. Create the successor first so the
        # revoke can atomically point at it, then claim the rotation.
        new_raw = secrets.token_urlsafe(48)
        new = session_dao.create(
            session, user_id=s.user_id, refresh_token=new_raw,
            user_agent=user_agent, ip_address=ip_address,
            family_id=s.family_id, expires_at=s.expires_at,
        )
        claimed = session_dao.revoke_for_rotation(
            session, session_id=s.id, replaced_by=new.id, now=now
        )
        if claimed == 1:
            return new_raw, new
        # Lost a concurrent rotation race: roll back our successor and re-read
        # the (now revoked) row to handle it via the grace/theft path below.
        session.delete(new)
        session.flush()
        s = session_dao.get_by_token_any_state(session, raw_token)
        if s is None:
            raise InvalidToken("refresh token invalid")

    # s is revoked (at lookup, or we just lost the race).
    if s.replaced_by is not None:
        revoked_at = _as_aware(s.revoked_at)
        within_grace = (
            revoked_at is not None
            and (now - revoked_at).total_seconds() <= settings.REFRESH_ROTATION_GRACE_SECONDS
        )
        if within_grace:
            # Benign concurrent retry: hand this caller a fresh sibling in the
            # same family so the just-rotated tab stays signed in.
            sib_raw = secrets.token_urlsafe(48)
            sib = session_dao.create(
                session, user_id=s.user_id, refresh_token=sib_raw,
                user_agent=user_agent, ip_address=ip_address,
                family_id=s.family_id, expires_at=s.expires_at,
            )
            return sib_raw, sib
        # Replay of a long-rotated token -> theft. Burn the whole family.
        session_dao.revoke_family(session, s.family_id, now=now)
        raise RefreshTokenReuse(user_id=s.user_id, family_id=s.family_id)

    # Explicitly revoked (logout / password change): plain invalid, no alarm.
    raise InvalidToken("refresh token revoked")


def revoke_refresh_token(session: Session, raw_token: str) -> None:
    """Mark the session as revoked. Idempotent — unknown tokens are no-ops."""
    session_dao.revoke_by_token(session, raw_token)
