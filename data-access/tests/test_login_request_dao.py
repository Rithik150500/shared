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
