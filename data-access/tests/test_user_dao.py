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


# --- sub-project D: email->phone grace helpers (SQLite-backed; no Postgres) ---


def test_get_by_email_roundtrip(db_session):
    u = User(email="grace@example.com", phone=None, password_hash="bcrypt")
    db_session.add(u)
    db_session.flush()
    assert str(user_dao.get_by_email(db_session, "grace@example.com").id) == str(u.id)
    assert user_dao.get_by_email(db_session, "nobody@example.com") is None


def test_set_phone_attaches_to_existing_user(db_session):
    u = User(email="grace@example.com", phone=None)
    db_session.add(u)
    db_session.flush()
    returned = user_dao.set_phone(db_session, u.id, "+919800000010")
    assert returned.id == u.id
    assert user_dao.get_by_id(db_session, u.id).phone == "+919800000010"


def test_set_phone_rejects_phone_taken_by_another(db_session):
    other = User(phone="+919800000011")
    db_session.add(other)
    db_session.flush()
    me = User(email="grace@example.com", phone=None)
    db_session.add(me)
    db_session.flush()
    with pytest.raises(ValueError, match="in use"):
        user_dao.set_phone(db_session, me.id, "+919800000011")
    assert user_dao.get_by_id(db_session, me.id).phone is None


def test_set_phone_allows_user_to_keep_their_own_phone(db_session):
    u = User(email="grace@example.com", phone="+919800000012")
    db_session.add(u)
    db_session.flush()
    user_dao.set_phone(db_session, u.id, "+919800000012")  # not "in use by another"
    assert user_dao.get_by_id(db_session, u.id).phone == "+919800000012"
