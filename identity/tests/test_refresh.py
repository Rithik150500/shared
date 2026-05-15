import pytest

from data_access.daos import user_dao
from identity.errors import InvalidToken
from identity.session.refresh import (
    consume_refresh_token,
    issue_refresh_token,
    revoke_refresh_token,
)


def test_issue_returns_raw_token_and_session(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    raw, sess = issue_refresh_token(postgresql_session, user_id=u.id)
    assert len(raw) >= 32  # opaque URL-safe; settings.REFRESH_TTL_DAYS uses default
    assert sess.user_id == u.id
    assert sess.revoked_at is None


def test_issue_with_user_agent_and_ip(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    raw, sess = issue_refresh_token(
        postgresql_session, user_id=u.id, user_agent="Mozilla/5.0", ip_address="203.0.113.5"
    )
    assert sess.user_agent == "Mozilla/5.0"
    assert sess.ip_address is not None


def test_consume_returns_session_for_valid_token(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    raw, _ = issue_refresh_token(postgresql_session, user_id=u.id)
    s = consume_refresh_token(postgresql_session, raw)
    assert s.user_id == u.id


def test_consume_invalid_token_raises(postgresql_session):
    with pytest.raises(InvalidToken):
        consume_refresh_token(postgresql_session, "this-token-was-never-issued")


def test_consume_after_revoke_raises(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    raw, _ = issue_refresh_token(postgresql_session, user_id=u.id)
    revoke_refresh_token(postgresql_session, raw)
    with pytest.raises(InvalidToken):
        consume_refresh_token(postgresql_session, raw)


def test_consume_touches_last_used(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    raw, sess = issue_refresh_token(postgresql_session, user_id=u.id)
    original = sess.last_used_at
    # consume — should bump last_used_at via session_dao.touch_last_used
    consume_refresh_token(postgresql_session, raw)
    postgresql_session.refresh(sess)
    assert sess.last_used_at >= original


def test_revoke_unknown_token_is_noop(postgresql_session):
    # No error — revoke is best-effort idempotent
    revoke_refresh_token(postgresql_session, "unknown-token")
