import uuid

import pytest

from data_access.daos import user_dao
from data_access.models import User, UserMunshi, UserNowlez


def test_get_or_create_by_phone_creates_user(postgresql_session):
    user, was_created = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210", locale="hi")
    assert was_created is True
    assert user.phone == "+919876543210"
    assert user.locale == "hi"
    assert isinstance(user.id, uuid.UUID)


def test_get_or_create_by_phone_returns_existing(postgresql_session):
    u1, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210", locale="en")
    u2, was_created = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210", locale="en")
    assert was_created is False
    assert u1.id == u2.id


def test_get_by_phone_returns_none_when_missing(postgresql_session):
    assert user_dao.get_by_phone(postgresql_session, "+91nope") is None


def test_ensure_munshi_extension_idempotent(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210", locale="en")
    user_dao.ensure_munshi_extension(postgresql_session, u.id)
    user_dao.ensure_munshi_extension(postgresql_session, u.id)  # second call no-ops
    rows = postgresql_session.query(UserMunshi).filter_by(user_id=u.id).count()
    assert rows == 1


def test_ensure_nowlez_extension_requires_name(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210", locale="en")
    user_dao.ensure_nowlez_extension(postgresql_session, u.id, name="Adrika Singh")
    rows = postgresql_session.query(UserNowlez).filter_by(user_id=u.id).count()
    assert rows == 1


def test_ensure_munshi_extension_raises_if_phone_null(postgresql_session):
    u = User(phone=None, email="x@example.com")
    postgresql_session.add(u)
    postgresql_session.flush()
    with pytest.raises(ValueError, match="phone"):
        user_dao.ensure_munshi_extension(postgresql_session, u.id)


def test_update_password(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    user_dao.update_password(postgresql_session, u.id, "$argon2id$dummy_hash")
    u2 = user_dao.get_by_id(postgresql_session, u.id)
    assert u2.password_hash == "$argon2id$dummy_hash"


def test_touch_last_login_updates_timestamp(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    assert u.last_login_at is None
    user_dao.touch_last_login(postgresql_session, u.id)
    postgresql_session.refresh(u)
    assert u.last_login_at is not None
