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


def test_merge_users_survivor_keeps_phone_gains_email(db_session):
    from datetime import datetime, timedelta, timezone

    from data_access.models import User

    # Survivor = older created_at (phone-only).
    older = datetime.now(timezone.utc) - timedelta(days=10)
    survivor = User(phone="+919876533333", created_at=older)
    db_session.add(survivor)
    db_session.flush()

    # Absorbed = newer (email-only) with a nowlez extension.
    absorbed, _ = user_dao.get_or_create_by_email(db_session, email="merge@example.com")
    user_dao.ensure_nowlez_extension(db_session, absorbed.id, name="Merge User")
    user_dao.set_email_verified(db_session, absorbed.id)

    user_dao.merge_users(db_session, survivor_id=survivor.id, absorbed_id=absorbed.id)

    # Absorbed user row is gone.
    assert db_session.get(User, absorbed.id) is None
    # Survivor now owns the email and keeps the phone.
    refreshed = db_session.get(User, survivor.id)
    assert refreshed.phone == "+919876533333"
    assert refreshed.email == "merge@example.com"
    # The nowlez extension was re-pointed to the survivor.
    assert user_dao.has_nowlez_extension(db_session, survivor.id) is True
    assert user_dao.is_email_verified(db_session, survivor.id) is True


def test_merge_users_no_extension_clash(db_session):
    from datetime import datetime, timedelta, timezone

    from data_access.models import User

    older = datetime.now(timezone.utc) - timedelta(days=5)
    survivor = User(phone="+919876544444", created_at=older)
    db_session.add(survivor)
    db_session.flush()
    user_dao.ensure_nowlez_extension(db_session, survivor.id, name="Survivor")

    absorbed, _ = user_dao.get_or_create_by_email(db_session, email="clash@example.com")
    user_dao.ensure_nowlez_extension(db_session, absorbed.id, name="Absorbed")

    # Survivor already has an extension; merge must NOT raise (absorbed's
    # extension is dropped, survivor's kept) and the absorbed row is removed.
    user_dao.merge_users(db_session, survivor_id=survivor.id, absorbed_id=absorbed.id)
    assert db_session.get(User, absorbed.id) is None
    assert db_session.get(User, survivor.id).email == "clash@example.com"


def test_merge_users_refuses_when_absorbed_owns_child_data(db_session):
    # D4 safety: the absorbed row is hard-deleted and cases/billing/munshi FK
    # users.id with ondelete=CASCADE. merge_users must REFUSE (not silently
    # destroy) an absorbed account that owns such data.
    import pytest
    from datetime import datetime, timedelta, timezone

    from data_access.models import User
    from data_access.daos.user_dao import MergeUnsafeError

    older = datetime.now(timezone.utc) - timedelta(days=3)
    survivor = User(phone="+919876555555", created_at=older)
    db_session.add(survivor)
    db_session.flush()

    # Absorbed owns a munshi (bot) identity — irreplaceable.
    absorbed, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876556666")
    user_dao.ensure_munshi_extension(db_session, absorbed.id)
    db_session.flush()

    with pytest.raises(MergeUnsafeError):
        user_dao.merge_users(
            db_session, survivor_id=survivor.id, absorbed_id=absorbed.id
        )
    # Nothing destroyed: both rows survive.
    assert db_session.get(User, absorbed.id) is not None
    assert db_session.get(User, survivor.id) is not None
