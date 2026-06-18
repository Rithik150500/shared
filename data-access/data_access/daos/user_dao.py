from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import User, UserMunshi, UserNowlez

_IST = ZoneInfo("Asia/Kolkata")


def get_or_create_by_phone(
    session: Session, *, phone: str, locale: str = "en"
) -> tuple[User, bool]:
    user = session.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
    if user is not None:
        return user, False
    user = User(phone=phone, locale=locale)
    session.add(user)
    session.flush()
    return user, True


def get_by_phone(session: Session, phone: str) -> User | None:
    return session.execute(select(User).where(User.phone == phone)).scalar_one_or_none()


def get_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def ensure_munshi_extension(session: Session, user_id: uuid.UUID) -> UserMunshi:
    existing = session.get(UserMunshi, user_id)
    if existing is not None:
        return existing
    user = session.get(User, user_id)
    if user is None or user.phone is None:
        raise ValueError("ensure_munshi_extension requires user with non-null phone")
    # billing_anniversary_date is NOT NULL on Postgres (sub-project E billing).
    # Anchor a new user's billing anniversary to their signup date (IST), matching
    # the migration backfill (created_at::date) and case_billing's creation path.
    # Without this, the INSERT violates the NOT NULL constraint in prod and every
    # brand-new user (e.g. a broadcast recipient) fails to onboard.
    ext = UserMunshi(
        user_id=user_id,
        billing_anniversary_date=datetime.now(timezone.utc).astimezone(_IST).date(),
    )
    session.add(ext)
    session.flush()
    return ext


def ensure_nowlez_extension(session: Session, user_id: uuid.UUID, *, name: str) -> UserNowlez:
    existing = session.get(UserNowlez, user_id)
    if existing is not None:
        return existing
    ext = UserNowlez(user_id=user_id, name=name)
    session.add(ext)
    session.flush()
    return ext


def update_password(session: Session, user_id: uuid.UUID, password_hash: str) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.password_hash = password_hash
    session.flush()


def touch_last_login(session: Session, user_id: uuid.UUID) -> None:
    from sqlalchemy import func
    user = session.get(User, user_id)
    if user is not None:
        user.last_login_at = func.now()
        session.flush()


def set_active(session: Session, user_id: uuid.UUID, is_active: bool) -> None:
    user = session.get(User, user_id)
    if user is not None:
        user.is_active = is_active
        session.flush()


def has_munshi_extension(session: Session, user_id: uuid.UUID) -> bool:
    """True iff the user has a row in users_munshi (i.e. is a Munshi user)."""
    return session.get(UserMunshi, user_id) is not None


def has_nowlez_extension(session: Session, user_id: uuid.UUID) -> bool:
    """True iff the user has a row in users_nowlez (i.e. is a Nowlez user)."""
    return session.get(UserNowlez, user_id) is not None


def count_munshi_users(session: Session) -> int:
    return session.execute(select(func.count()).select_from(UserMunshi)).scalar_one()


def count_munshi_onboarded(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(UserMunshi).where(UserMunshi.onboarded_at.is_not(None))
    ).scalar_one()


def count_munshi_active_since(session: Session, since: datetime) -> int:
    """Count Munshi users whose last_message_at >= since (rows with NULL last_message_at are excluded)."""
    return session.execute(
        select(func.count()).select_from(UserMunshi).where(UserMunshi.last_message_at >= since)
    ).scalar_one()


def list_munshi_users(
    session: Session, *, limit: int = 50, offset: int = 0, search: str | None = None
) -> list[tuple[User, UserMunshi]]:
    stmt = select(User, UserMunshi).join(UserMunshi, UserMunshi.user_id == User.id)
    if search:
        stmt = stmt.where(User.phone.ilike(f"%{search}%"))
    stmt = stmt.order_by(UserMunshi.created_at.desc()).limit(limit).offset(offset)
    return list(session.execute(stmt).tuples().all())
