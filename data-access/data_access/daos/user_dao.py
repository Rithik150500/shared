from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User, UserMunshi, UserNowlez


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


def get_by_email(session: Session, email: str) -> User | None:
    return session.execute(select(User).where(User.email == email)).scalar_one_or_none()


def get_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def set_phone(session: Session, user_id: uuid.UUID, phone: str) -> User:
    """Attach (or replace) a user's phone number.

    Used by the sub-project D email->phone grace flow, where a migrated
    email-only user (phone=NULL) links a phone to their existing account.
    Raises ValueError if the phone already belongs to a *different* user
    (users.phone is UNIQUE), or if the user does not exist.
    """
    existing = session.execute(
        select(User).where(User.phone == phone)
    ).scalar_one_or_none()
    # str()-normalize: the SQLite test variant returns ids as str, Postgres as
    # UUID; compare canonically so "user keeps their own phone" isn't misread
    # as a collision.
    if existing is not None and str(existing.id) != str(user_id):
        raise ValueError("phone already in use by another account")
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.phone = phone
    session.flush()
    return user


def ensure_munshi_extension(session: Session, user_id: uuid.UUID) -> UserMunshi:
    existing = session.get(UserMunshi, user_id)
    if existing is not None:
        return existing
    user = session.get(User, user_id)
    if user is None or user.phone is None:
        raise ValueError("ensure_munshi_extension requires user with non-null phone")
    ext = UserMunshi(user_id=user_id)
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
