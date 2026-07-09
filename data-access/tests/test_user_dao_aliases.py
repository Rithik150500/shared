from __future__ import annotations

from datetime import datetime, timezone

from data_access.daos import user_dao
from data_access.models import User, UserIdentity


def _owner_with_phone(db_session, phone):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone=phone)
    return user


def _add_identity(db_session, user_id, kind, value, *, verified):
    row = UserIdentity(
        user_id=user_id,
        kind=kind,
        value=value,
        verified_at=datetime.now(timezone.utc) if verified else None,
        added_by="operator",
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_verified_phone_alias_routes_to_owner(db_session):
    owner = _owner_with_phone(db_session, "+919953652710")
    _add_identity(db_session, owner.id, "phone", "+918882271502", verified=True)

    user, created = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    assert created is False
    assert user.id == owner.id
    # No new user row was spawned.
    assert db_session.query(User).count() == 1


def test_unverified_phone_alias_does_not_route(db_session):
    owner = _owner_with_phone(db_session, "+919953652710")
    _add_identity(db_session, owner.id, "phone", "+918882271502", verified=False)

    user, created = user_dao.get_or_create_by_phone(db_session, phone="+918882271502")
    assert created is True  # pending alias is ignored -> a fresh user is created
    assert user.id != owner.id
    assert db_session.query(User).count() == 2


def test_resolve_verified_email_alias_hit_and_miss(db_session):
    owner = _owner_with_phone(db_session, "+919953652710")
    _add_identity(db_session, owner.id, "email", "nitishv245@gmail.com", verified=True)

    hit = user_dao.resolve_verified_email_alias(db_session, "  NitishV245@Gmail.com ")
    assert hit is not None and hit.id == owner.id
    assert user_dao.resolve_verified_email_alias(db_session, "someone@else.com") is None
