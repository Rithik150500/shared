from __future__ import annotations

import uuid
from datetime import timedelta

from data_access.daos import email_otp_dao


def test_insert_email_otp(db_session):
    o = email_otp_dao.insert(
        db_session,
        email="adrika@example.com",
        code_hash="$argon2id$v=19$dummy",
        ttl_minutes=10,
        ip_address="203.0.113.5",
    )
    assert isinstance(o.id, uuid.UUID)
    assert o.email == "adrika@example.com"
    assert o.attempts_remaining == 3
    assert o.delivery_status == "pending"
    assert (o.expires_at - o.created_at) <= timedelta(minutes=10, seconds=5) if o.created_at else True


def test_get_by_id(db_session):
    o = email_otp_dao.insert(db_session, email="a@b.com", code_hash="x", ttl_minutes=10)
    assert email_otp_dao.get_by_id(db_session, o.id).id == o.id


def test_get_by_id_unknown_none(db_session):
    assert email_otp_dao.get_by_id(db_session, uuid.uuid4()) is None


def test_get_active_returns_most_recent(db_session):
    email_otp_dao.insert(db_session, email="a@b.com", code_hash="old", ttl_minutes=10)
    o2 = email_otp_dao.insert(db_session, email="a@b.com", code_hash="new", ttl_minutes=10)
    active = email_otp_dao.get_active(db_session, "a@b.com")
    assert active is not None
    assert active.id == o2.id


def test_get_active_none_when_expired(db_session):
    email_otp_dao.insert(db_session, email="a@b.com", code_hash="x", ttl_minutes=-5)
    assert email_otp_dao.get_active(db_session, "a@b.com") is None


def test_mark_delivered(db_session):
    o = email_otp_dao.insert(db_session, email="a@b.com", code_hash="x", ttl_minutes=10)
    email_otp_dao.mark_delivered(db_session, o.id, provider_id="resend_abc")
    o2 = email_otp_dao.get_by_id(db_session, o.id)
    assert o2.delivery_status == "delivered"
    assert o2.delivery_provider_id == "resend_abc"


def test_mark_failed(db_session):
    o = email_otp_dao.insert(db_session, email="a@b.com", code_hash="x", ttl_minutes=10)
    email_otp_dao.mark_failed(db_session, o.id)
    assert email_otp_dao.get_by_id(db_session, o.id).delivery_status == "failed"


def test_decrement_attempts(db_session):
    o = email_otp_dao.insert(db_session, email="a@b.com", code_hash="x", ttl_minutes=10)
    assert email_otp_dao.decrement_attempts(db_session, o.id) == 2


def test_decrement_attempts_floors_at_zero(db_session):
    o = email_otp_dao.insert(db_session, email="a@b.com", code_hash="x", ttl_minutes=10)
    for _ in range(5):
        email_otp_dao.decrement_attempts(db_session, o.id)
    assert email_otp_dao.get_by_id(db_session, o.id).attempts_remaining == 0
