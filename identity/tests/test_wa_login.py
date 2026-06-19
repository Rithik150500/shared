import pytest

from data_access.daos import login_request_dao, session_dao, user_dao
from data_access.models import AuditLog
from identity import api as identity_api


def _audit_types(session):
    return [a.event_type for a in session.query(AuditLog).all()]


def test_start_wa_login_returns_nonce_poll_secret_and_pending_row(db_session):
    out = identity_api.start_wa_login(db_session, brand="nowlez", ip_address="203.0.113.5", user_agent="UA")
    db_session.flush()
    assert set(out) >= {"login_id", "nonce", "poll_secret", "expires_at"}
    row = login_request_dao.get_active_by_token(
        db_session, token_hash=session_dao._hash(out["nonce"])
    )
    assert row is not None
    assert row.status == "pending"
    assert row.direction == "web2bot"
    assert row.user_id is None
    assert "wa_login.started" in _audit_types(db_session)


def test_confirm_wa_login_flips_pending_to_confirmed(db_session):
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.flush()
    out = identity_api.start_wa_login(db_session, brand="nowlez")
    db_session.flush()
    flipped = identity_api.confirm_wa_login(db_session, nonce=out["nonce"], user=u, brand="munshi")
    assert flipped is True
    row = login_request_dao.get_by_id(db_session, __import__("uuid").UUID(out["login_id"]))
    assert row.status == "confirmed"
    assert row.user_id == u.id
    assert "wa_login.confirmed" in _audit_types(db_session)


def test_confirm_wa_login_second_call_is_noop(db_session):
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.flush()
    out = identity_api.start_wa_login(db_session, brand="nowlez")
    db_session.flush()
    assert identity_api.confirm_wa_login(db_session, nonce=out["nonce"], user=u, brand="munshi") is True
    assert identity_api.confirm_wa_login(db_session, nonce=out["nonce"], user=u, brand="munshi") is False


def test_confirm_wa_login_unknown_nonce_returns_false(db_session):
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.flush()
    assert identity_api.confirm_wa_login(db_session, nonce="never-issued", user=u, brand="munshi") is False


def test_start_wa_login_from_bot_returns_raw_nonce_and_pending_row(db_session):
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.flush()
    nonce = identity_api.start_wa_login_from_bot(db_session, user_id=u.id, brand="munshi")
    db_session.flush()
    assert isinstance(nonce, str) and len(nonce) >= 20
    row = login_request_dao.get_active_by_token(db_session, token_hash=session_dao._hash(nonce))
    assert row is not None
    assert row.direction == "bot2web"
    assert row.status == "pending"
    assert row.user_id == u.id
    assert row.phone == "+919876543210"
