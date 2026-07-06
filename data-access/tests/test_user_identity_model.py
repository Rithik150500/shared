from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from data_access.models import User, UserIdentity


def _make_user(db_session, phone="+919000000001"):
    u = User(phone=phone)
    db_session.add(u)
    db_session.flush()
    return u


def test_user_identity_insert_and_read(db_session):
    u = _make_user(db_session)
    row = UserIdentity(user_id=u.id, kind="phone", value="+919888800001", added_by="operator")
    db_session.add(row)
    db_session.flush()
    got = db_session.execute(
        select(UserIdentity).where(UserIdentity.value == "+919888800001")
    ).scalar_one()
    assert got.user_id == u.id
    assert got.kind == "phone"
    assert got.verified_at is None  # pending by default


def test_user_identity_unique_kind_value(db_session):
    u1 = _make_user(db_session, phone="+919000000011")
    u2 = _make_user(db_session, phone="+919000000012")
    db_session.add(UserIdentity(user_id=u1.id, kind="email", value="dup@example.com"))
    db_session.flush()
    db_session.add(UserIdentity(user_id=u2.id, kind="email", value="dup@example.com"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_identity_cascades_on_user_delete(db_session):
    # SQLite doesn't enforce FK constraints (incl. ON DELETE CASCADE) unless
    # explicitly turned on per-connection; the shared db_session fixture
    # doesn't set this, so opt in locally for this one cascade-behavior test.
    db_session.execute(text("PRAGMA foreign_keys=ON"))
    u = _make_user(db_session, phone="+919000000021")
    db_session.add(UserIdentity(user_id=u.id, kind="phone", value="+919888800021"))
    db_session.flush()
    db_session.delete(u)
    db_session.flush()
    remaining = db_session.execute(
        select(UserIdentity).where(UserIdentity.value == "+919888800021")
    ).scalar_one_or_none()
    assert remaining is None
