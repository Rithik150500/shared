import uuid
from datetime import datetime, timedelta, timezone

from data_access.daos import otp_dao
from data_access.models import OtpCode


def test_insert_otp(postgresql_session):
    o = otp_dao.insert(
        postgresql_session,
        phone="+919876543210",
        code_hash="$argon2id$v=19$dummy",
        channel="whatsapp",
        ttl_minutes=10,
        ip_address="203.0.113.5",
    )
    assert isinstance(o.id, uuid.UUID)
    assert o.attempts_remaining == 3
    assert o.delivery_status == "pending"
    assert (o.expires_at - o.created_at) <= timedelta(minutes=10, seconds=5)


def test_get_by_id_returns_otp(postgresql_session):
    o = otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=10)
    found = otp_dao.get_by_id(postgresql_session, o.id)
    assert found is not None
    assert found.id == o.id


def test_get_by_id_returns_none_for_unknown(postgresql_session):
    assert otp_dao.get_by_id(postgresql_session, uuid.uuid4()) is None


def test_mark_delivered(postgresql_session):
    o = otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=10)
    otp_dao.mark_delivered(postgresql_session, o.id, provider_id="wamid.abc123")
    o2 = otp_dao.get_by_id(postgresql_session, o.id)
    assert o2.delivery_status == "delivered"
    assert o2.delivery_provider_id == "wamid.abc123"


def test_mark_failed(postgresql_session):
    o = otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=10)
    otp_dao.mark_failed(postgresql_session, o.id)
    o2 = otp_dao.get_by_id(postgresql_session, o.id)
    assert o2.delivery_status == "failed"


def test_mark_used(postgresql_session):
    o = otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=10)
    otp_dao.mark_used(postgresql_session, o.id)
    o2 = otp_dao.get_by_id(postgresql_session, o.id)
    assert o2.used_at is not None


def test_decrement_attempts(postgresql_session):
    o = otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=10)
    remaining = otp_dao.decrement_attempts(postgresql_session, o.id)
    assert remaining == 2


def test_decrement_attempts_floors_at_zero(postgresql_session):
    o = otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=10)
    for _ in range(5):
        otp_dao.decrement_attempts(postgresql_session, o.id)
    o2 = otp_dao.get_by_id(postgresql_session, o.id)
    assert o2.attempts_remaining == 0


def test_get_active_returns_most_recent(postgresql_session):
    otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="old", channel="whatsapp", ttl_minutes=10)
    o2 = otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="new", channel="whatsapp", ttl_minutes=10)
    active = otp_dao.get_active(postgresql_session, phone="+919876543210")
    assert active is not None
    assert active.id == o2.id


def test_get_active_returns_none_when_all_expired(postgresql_session):
    otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="x", channel="whatsapp", ttl_minutes=-5)
    assert otp_dao.get_active(postgresql_session, phone="+919876543210") is None


def test_count_within_window(postgresql_session):
    for _ in range(3):
        otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="x", channel="whatsapp", ttl_minutes=10)
    n = otp_dao.count_within(postgresql_session, phone="+919876543210", minutes=60)
    assert n == 3


def test_count_by_ip_within(postgresql_session):
    for i in range(2):
        otp_dao.insert(postgresql_session, phone=f"+9198765432{i:02d}", code_hash="x", channel="whatsapp", ttl_minutes=10, ip_address="203.0.113.5")
    n = otp_dao.count_by_ip_within(postgresql_session, ip_address="203.0.113.5", minutes=60)
    assert n == 2


def test_cleanup_expired(postgresql_session):
    otp_dao.insert(postgresql_session, phone="+91...", code_hash="x", channel="whatsapp", ttl_minutes=-2000)
    n = otp_dao.cleanup_expired(postgresql_session, older_than_hours=24)
    assert n >= 1


def test_mark_used_atomic_single_use(db_session):
    # §10 atomic single-use: first call wins (rowcount 1), replay loses (0).
    o = otp_dao.insert(
        db_session, phone="+919876500099", code_hash="x", channel="whatsapp", ttl_minutes=10
    )
    db_session.flush()
    assert otp_dao.mark_used_atomic(db_session, o.id) == 1
    assert otp_dao.mark_used_atomic(db_session, o.id) == 0


def test_mark_used_atomic_expired_returns_zero(db_session):
    o = otp_dao.insert(
        db_session, phone="+919876500098", code_hash="x", channel="whatsapp", ttl_minutes=-5
    )
    db_session.flush()
    assert otp_dao.mark_used_atomic(db_session, o.id) == 0
