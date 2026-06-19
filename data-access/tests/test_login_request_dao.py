from __future__ import annotations

import uuid

from data_access.daos import login_request_dao
from data_access.models import User


def _make_user(db_session, phone="+919876500001"):
    u = User(phone=phone, locale="en")
    db_session.add(u)
    db_session.flush()
    return u


def test_create_web2bot_pending_no_user(db_session):
    lr = login_request_dao.create_web2bot(
        db_session,
        token_hash="h1",
        brand="nowlez",
        poll_bind_hash="pbh1",
        ttl_seconds=300,
        ip_address="203.0.113.5",
        user_agent="UA",
    )
    assert lr.direction == "web2bot"
    assert lr.status == "pending"
    assert lr.user_id is None
    assert lr.poll_bind_hash == "pbh1"
    assert lr.expires_at is not None


def test_create_bot2web_pending_with_user(db_session):
    u = _make_user(db_session)
    lr = login_request_dao.create_bot2web(
        db_session,
        token_hash="h2",
        brand="munshi",
        user_id=u.id,
        phone=u.phone,
        ttl_seconds=120,
    )
    assert lr.direction == "bot2web"
    assert lr.status == "pending"
    assert lr.user_id == u.id
    assert lr.phone == u.phone


def test_get_active_by_token_returns_pending(db_session):
    login_request_dao.create_web2bot(
        db_session, token_hash="h3", brand="nowlez", poll_bind_hash="p", ttl_seconds=300
    )
    found = login_request_dao.get_active_by_token(db_session, token_hash="h3")
    assert found is not None
    assert found.token_hash == "h3"


def test_get_active_by_token_excludes_expired(db_session):
    login_request_dao.create_web2bot(
        db_session, token_hash="h4", brand="nowlez", poll_bind_hash="p", ttl_seconds=-10
    )
    assert login_request_dao.get_active_by_token(db_session, token_hash="h4") is None


def test_get_by_id_roundtrip(db_session):
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="h5", brand="nowlez", poll_bind_hash="p", ttl_seconds=300
    )
    again = login_request_dao.get_by_id(db_session, lr.id)
    assert again is not None
    assert again.id == lr.id


def test_get_by_id_unknown_returns_none(db_session):
    assert login_request_dao.get_by_id(db_session, uuid.uuid4()) is None


def test_confirm_flips_pending_to_confirmed_returns_one(db_session):
    u = _make_user(db_session, phone="+919876500010")
    login_request_dao.create_web2bot(
        db_session, token_hash="hc1", brand="nowlez", poll_bind_hash="p", ttl_seconds=300
    )
    rc = login_request_dao.confirm(
        db_session, token_hash="hc1", user_id=u.id, phone=u.phone
    )
    assert rc == 1
    row = login_request_dao.get_by_id(
        db_session,
        login_request_dao.get_active_by_token(db_session, token_hash="hc1").id,
    )
    assert row.status == "confirmed"
    assert str(row.user_id) == str(u.id)
    assert row.phone == u.phone


def test_confirm_second_call_returns_zero(db_session):
    u = _make_user(db_session, phone="+919876500011")
    login_request_dao.create_web2bot(
        db_session, token_hash="hc2", brand="nowlez", poll_bind_hash="p", ttl_seconds=300
    )
    assert login_request_dao.confirm(db_session, token_hash="hc2", user_id=u.id, phone=u.phone) == 1
    # Already confirmed — second confirm is a no-op (rowcount 0).
    assert login_request_dao.confirm(db_session, token_hash="hc2", user_id=u.id, phone=u.phone) == 0


def test_confirm_rejects_expired(db_session):
    u = _make_user(db_session, phone="+919876500012")
    login_request_dao.create_web2bot(
        db_session, token_hash="hc3", brand="nowlez", poll_bind_hash="p", ttl_seconds=-10
    )
    assert login_request_dao.confirm(db_session, token_hash="hc3", user_id=u.id, phone=u.phone) == 0


def test_confirm_unknown_token_returns_zero(db_session):
    u = _make_user(db_session, phone="+919876500013")
    assert login_request_dao.confirm(db_session, token_hash="nope", user_id=u.id, phone=u.phone) == 0


def test_consume_by_id_happy_returns_user_id(db_session):
    u = _make_user(db_session, phone="+919876500020")
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hk1", brand="nowlez", poll_bind_hash="pbh-ok", ttl_seconds=300
    )
    login_request_dao.confirm(db_session, token_hash="hk1", user_id=u.id, phone=u.phone)
    got = login_request_dao.consume_by_id(db_session, login_id=lr.id, poll_bind_hash="pbh-ok")
    assert str(got) == str(u.id)
    row = login_request_dao.get_by_id(db_session, lr.id)
    assert row.status == "consumed"
    assert row.consumed_at is not None


def test_consume_by_id_replay_returns_none(db_session):
    u = _make_user(db_session, phone="+919876500021")
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hk2", brand="nowlez", poll_bind_hash="pbh-ok", ttl_seconds=300
    )
    login_request_dao.confirm(db_session, token_hash="hk2", user_id=u.id, phone=u.phone)
    assert str(login_request_dao.consume_by_id(db_session, login_id=lr.id, poll_bind_hash="pbh-ok")) == str(u.id)
    # Replay: already consumed -> None.
    assert login_request_dao.consume_by_id(db_session, login_id=lr.id, poll_bind_hash="pbh-ok") is None


def test_consume_by_id_wrong_poll_bind_returns_none(db_session):
    u = _make_user(db_session, phone="+919876500022")
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hk3", brand="nowlez", poll_bind_hash="pbh-real", ttl_seconds=300
    )
    login_request_dao.confirm(db_session, token_hash="hk3", user_id=u.id, phone=u.phone)
    assert login_request_dao.consume_by_id(db_session, login_id=lr.id, poll_bind_hash="pbh-WRONG") is None
    # Still confirmed (not consumed) because the bind didn't match.
    assert login_request_dao.get_by_id(db_session, lr.id).status == "confirmed"


def test_consume_by_id_not_confirmed_returns_none(db_session):
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hk4", brand="nowlez", poll_bind_hash="pbh", ttl_seconds=300
    )
    # Still pending (never confirmed) -> cannot consume.
    assert login_request_dao.consume_by_id(db_session, login_id=lr.id, poll_bind_hash="pbh") is None


def test_consume_by_token_happy_then_replay(db_session):
    u = _make_user(db_session, phone="+919876500023")
    login_request_dao.create_bot2web(
        db_session, token_hash="ht1", brand="munshi", user_id=u.id, phone=u.phone, ttl_seconds=120
    )
    # bot2web rows are flipped to confirmed by the handler after a successful send.
    login_request_dao.confirm(db_session, token_hash="ht1", user_id=u.id, phone=u.phone)
    assert str(login_request_dao.consume_by_token(db_session, token_hash="ht1")) == str(u.id)
    assert login_request_dao.consume_by_token(db_session, token_hash="ht1") is None


def test_consume_by_id_expired_confirmed_returns_none(db_session):
    # A nonce that was confirmed but then expired must NOT be consumable
    # (the consume UPDATE's WHERE clause includes expires_at > func.now()).
    from datetime import datetime, timezone

    from sqlalchemy import update as sa_update

    from data_access.models import LoginRequest

    u = _make_user(db_session, phone="+919876500030")
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hk5", brand="nowlez", poll_bind_hash="pbh", ttl_seconds=300
    )
    login_request_dao.confirm(db_session, token_hash="hk5", user_id=u.id, phone=u.phone)
    # Backdate expires_at to simulate a nonce that expired after confirm.
    db_session.execute(
        sa_update(LoginRequest)
        .where(LoginRequest.id == lr.id)
        .values(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    )
    db_session.flush()
    assert (
        login_request_dao.consume_by_id(db_session, login_id=lr.id, poll_bind_hash="pbh")
        is None
    )


def test_mark_expired_sets_status(db_session):
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hx1", brand="nowlez", poll_bind_hash="p", ttl_seconds=300
    )
    login_request_dao.mark_expired(db_session, lr.id)
    assert login_request_dao.get_by_id(db_session, lr.id).status == "expired"


def test_count_by_ip_within_counts_recent(db_session):
    for i in range(3):
        login_request_dao.create_web2bot(
            db_session,
            token_hash=f"hip{i}",
            brand="nowlez",
            poll_bind_hash="p",
            ttl_seconds=300,
            ip_address="203.0.113.9",
        )
    assert login_request_dao.count_by_ip_within(db_session, ip_address="203.0.113.9", minutes=60) == 3
    assert login_request_dao.count_by_ip_within(db_session, ip_address="203.0.113.99", minutes=60) == 0


def test_cleanup_expired_deletes_old_consumed_and_expired(db_session):
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone

    from data_access.models import LoginRequest

    old = datetime.now(timezone.utc) - timedelta(hours=48)
    stale = LoginRequest(
        token_hash="hcl1",
        direction="web2bot",
        status="consumed",
        brand="nowlez",
        expires_at=old,
        created_at=old,
        consumed_at=old,
    )
    db_session.add(stale)
    db_session.flush()
    n = login_request_dao.cleanup_expired(db_session, older_than_hours=24)
    assert n >= 1
    assert login_request_dao.get_by_id(db_session, stale.id) is None


def test_cleanup_expired_keeps_recent(db_session):
    lr = login_request_dao.create_web2bot(
        db_session, token_hash="hcl2", brand="nowlez", poll_bind_hash="p", ttl_seconds=300
    )
    login_request_dao.cleanup_expired(db_session, older_than_hours=24)
    # A fresh, still-valid pending row must survive.
    assert login_request_dao.get_by_id(db_session, lr.id) is not None
