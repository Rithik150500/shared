import hashlib
from datetime import datetime, timedelta, timezone

from data_access.daos import session_dao, user_dao


def test_create_session(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    s = session_dao.create(
        postgresql_session,
        user_id=u.id,
        refresh_token="opaque-token-12345",
        user_agent="Mozilla/5.0",
        ip_address="203.0.113.5",
        ttl_days=30,
    )
    assert s.user_id == u.id
    assert s.refresh_token_hash == hashlib.sha256(b"opaque-token-12345").hexdigest()
    assert s.revoked_at is None
    assert (s.expires_at - s.created_at) >= timedelta(days=29, hours=23)


def test_lookup_by_token(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    session_dao.create(postgresql_session, user_id=u.id, refresh_token="tok-1", ttl_days=30)
    s = session_dao.lookup_by_token(postgresql_session, "tok-1")
    assert s is not None
    assert s.user_id == u.id


def test_lookup_returns_none_for_unknown_token(postgresql_session):
    assert session_dao.lookup_by_token(postgresql_session, "nope") is None


def test_lookup_returns_none_for_revoked(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    session_dao.create(postgresql_session, user_id=u.id, refresh_token="tok-1", ttl_days=30)
    session_dao.revoke_by_token(postgresql_session, "tok-1")
    s = session_dao.lookup_by_token(postgresql_session, "tok-1")
    assert s is None


def test_lookup_returns_none_for_expired(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    session_dao.create(postgresql_session, user_id=u.id, refresh_token="tok-1", ttl_days=-1)
    s = session_dao.lookup_by_token(postgresql_session, "tok-1")
    assert s is None


def test_touch_last_used_updates_timestamp(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    s = session_dao.create(postgresql_session, user_id=u.id, refresh_token="tok-1", ttl_days=30)
    original = s.last_used_at
    # advance a tiny bit by triggering an update via touch_last_used
    session_dao.touch_last_used(postgresql_session, s.id)
    postgresql_session.refresh(s)
    assert s.last_used_at >= original


def test_revoke_all_except(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    s1 = session_dao.create(postgresql_session, user_id=u.id, refresh_token="tok-1", ttl_days=30)
    session_dao.create(postgresql_session, user_id=u.id, refresh_token="tok-2", ttl_days=30)
    session_dao.revoke_all_except(postgresql_session, u.id, except_session_id=s1.id)
    assert session_dao.lookup_by_token(postgresql_session, "tok-1") is not None
    assert session_dao.lookup_by_token(postgresql_session, "tok-2") is None


def test_cleanup_expired(postgresql_session):
    from datetime import datetime, timedelta, timezone
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    # Insert a long-expired row directly (manual ttl_days=-10 makes expires_at < cutoff)
    session_dao.create(postgresql_session, user_id=u.id, refresh_token="old-tok", ttl_days=-10)
    deleted = session_dao.cleanup_expired(postgresql_session, older_than_days=7)
    assert deleted >= 1
