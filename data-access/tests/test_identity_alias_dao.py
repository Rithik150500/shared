from __future__ import annotations

import pytest

from data_access.daos import identity_alias_dao, user_dao
from data_access.daos.identity_alias_dao import AliasConflictError
from data_access.models import User, UserIdentity


def _account(db_session, phone):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone=phone)
    user_dao.ensure_nowlez_extension(db_session, user.id, name="Acct")
    return user


def test_add_alias_force_verified_routes(db_session):
    acct = _account(db_session, "+919953652710")
    row = identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="phone", value="8882271502", verified=True
    )
    assert row.verified_at is not None
    assert row.value == "+918882271502"  # normalized
    routed, created = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    assert created is False and routed.id == acct.id


def test_add_alias_pending_does_not_route(db_session):
    acct = _account(db_session, "+919953652710")
    identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="phone", value="8882271502", verified=False
    )
    routed, created = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    assert created is True and routed.id != acct.id


def test_add_email_alias_canonicalizes(db_session):
    acct = _account(db_session, "+919953652710")
    row = identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="email", value="  NitishV245@Gmail.COM ",
        verified=True,
    )
    assert row.value == "nitishv245@gmail.com"


def test_add_alias_reclaims_empty_orphan(db_session):
    acct = _account(db_session, "+919953652710")
    # A dormant bot-spawned orphan owns +918882271502 as its PRIMARY (+ munshi state).
    orphan, _ = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    user_dao.ensure_munshi_extension(db_session, orphan.id)
    db_session.flush()
    orphan_id = orphan.id

    row = identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="phone", value="8882271502", verified=True
    )
    assert row.user_id == acct.id
    assert db_session.get(User, orphan_id) is None  # empty orphan absorbed
    routed, created = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    assert created is False and routed.id == acct.id


def test_add_alias_refuses_data_owning_account(db_session):
    from datetime import date

    from data_access.daos import case_dao
    from ecourts_client.models import Act, Case as DataCase, Party

    acct = _account(db_session, "+919953652710")
    other, _ = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    # Give the colliding account a real case via the tested upsert path (raw
    # Case(...) construction would trip unknown NOT-NULL columns).
    case_dao.upsert_case(
        db_session, user_id=other.id, cnr="MHCC010054732024",
        case_data=DataCase(
            cnr="MHCC010054732024", title="P vs D", court="DC Mumbai",
            stage="Pending", next_hearing_date=date(2026, 6, 15), judge="Hon.",
            parties=[Party(name="A", role="petitioner")],
            acts=[Act(act_name="CPC", section="9")],
        ),
    )
    db_session.flush()

    with pytest.raises(AliasConflictError):
        identity_alias_dao.add_alias(
            db_session, user_id=acct.id, kind="phone", value="8882271502", verified=True
        )
    assert db_session.get(User, other.id) is not None  # nothing destroyed


def test_add_alias_own_primary_is_noop(db_session):
    acct = _account(db_session, "+919953652710")
    assert identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="phone", value="9953652710", verified=True
    ) is None


def test_add_alias_reclaims_orphan_owning_value_as_alias(db_session):
    # The contested value is another (empty) account's ALIAS, not its primary.
    # Reclaim must free that alias row so the new INSERT succeeds WITHOUT relying
    # on DB-level cascade (the SQLite test fixture has FK enforcement off).
    acct = _account(db_session, "+919953652710")
    orphan = _account(db_session, "+919000000099")
    identity_alias_dao.add_alias(
        db_session, user_id=orphan.id, kind="email", value="shared@x.com", verified=True
    )
    orphan_id = orphan.id

    row = identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="email", value="shared@x.com", verified=True
    )
    assert row.user_id == acct.id
    assert db_session.get(User, orphan_id) is None  # empty orphan absorbed
    hit = user_dao.resolve_verified_email_alias(db_session, "shared@x.com")
    assert hit is not None and hit.id == acct.id


def test_verify_and_remove_and_list(db_session):
    acct = _account(db_session, "+919953652710")
    row = identity_alias_dao.add_alias(
        db_session, user_id=acct.id, kind="email", value="a@b.com", verified=False
    )
    assert row.verified_at is None
    identity_alias_dao.verify_alias(db_session, row.id)
    assert db_session.get(UserIdentity, row.id).verified_at is not None
    assert len(identity_alias_dao.list_aliases(db_session, acct.id)) == 1
    identity_alias_dao.remove_alias(db_session, row.id)
    assert identity_alias_dao.list_aliases(db_session, acct.id) == []
