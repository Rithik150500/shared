from __future__ import annotations

from data_access.daos import user_dao


def test_get_or_create_by_email_creates(db_session):
    user, created = user_dao.get_or_create_by_email(db_session, email="adrika@example.com")
    assert created is True
    assert user.email == "adrika@example.com"
    assert user.locale == "en"


def test_get_or_create_by_email_returns_existing(db_session):
    u1, _ = user_dao.get_or_create_by_email(db_session, email="adrika@example.com")
    u2, created = user_dao.get_or_create_by_email(db_session, email="adrika@example.com")
    assert created is False
    assert u1.id == u2.id


def test_get_by_email_hit_and_miss(db_session):
    user_dao.get_or_create_by_email(db_session, email="hit@example.com")
    assert user_dao.get_by_email(db_session, "hit@example.com") is not None
    assert user_dao.get_by_email(db_session, "miss@example.com") is None


def test_get_or_create_by_phone_hardened_create_and_existing(db_session):
    u1, created1 = user_dao.get_or_create_by_phone(db_session, phone="+919876511111", locale="hi")
    assert created1 is True
    assert u1.phone == "+919876511111"
    assert u1.locale == "hi"
    u2, created2 = user_dao.get_or_create_by_phone(db_session, phone="+919876511111", locale="hi")
    assert created2 is False
    assert u1.id == u2.id


def test_get_or_create_by_phone_no_duplicate_rows(db_session):
    from data_access.models import User

    for _ in range(3):
        user_dao.get_or_create_by_phone(db_session, phone="+919876522222")
    count = db_session.query(User).filter_by(phone="+919876522222").count()
    assert count == 1


def test_set_and_is_email_verified_roundtrip(db_session):
    user, _ = user_dao.get_or_create_by_email(db_session, email="verify@example.com")
    user_dao.ensure_nowlez_extension(db_session, user.id, name="Verify User")
    # Freshly-created extension defaults email_verified=False.
    assert user_dao.is_email_verified(db_session, user.id) is False
    user_dao.set_email_verified(db_session, user.id)
    assert user_dao.is_email_verified(db_session, user.id) is True


def test_is_email_verified_no_nowlez_extension_is_false(db_session):
    user, _ = user_dao.get_or_create_by_email(db_session, email="noext@example.com")
    # No users_nowlez row -> treat as unverified (None coerces to False).
    assert user_dao.is_email_verified(db_session, user.id) is False
