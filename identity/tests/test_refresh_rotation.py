"""Refresh-token ROTATION + reuse-detection (sqlite db_session, runs offline)."""
from datetime import datetime, timedelta, timezone

import pytest

from data_access.daos import session_dao, user_dao
from data_access.models import AuditLog
from identity import api as identity_api
from identity.config import settings
from identity.errors import InvalidToken, RefreshTokenReuse
from identity.session.refresh import (
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
)


def _user(db):
    u, _ = user_dao.get_or_create_by_email(db, email="rot@example.com")
    return u


def test_rotation_issues_new_token_and_revokes_old(db_session):
    u = _user(db_session)
    raw, s1 = issue_refresh_token(db_session, user_id=u.id)

    new_raw, s2 = rotate_refresh_token(db_session, raw)
    assert new_raw != raw
    assert s2.id != s1.id
    assert s2.family_id == s1.family_id  # same lineage
    assert s2.user_id == u.id
    # old row is revoked and points at its successor
    old = session_dao.get_by_token_any_state(db_session, raw)
    assert old.revoked_at is not None
    assert str(old.replaced_by) == str(s2.id)
    # old token no longer resolves as an ACTIVE session
    assert session_dao.lookup_by_token(db_session, raw) is None
    # the new token IS active and can rotate again
    new2_raw, s3 = rotate_refresh_token(db_session, new_raw)
    assert s3.family_id == s1.family_id


def test_rotation_preserves_absolute_expiry(db_session):
    u = _user(db_session)
    raw, s1 = issue_refresh_token(db_session, user_id=u.id)
    original_expiry = s1.expires_at
    _new_raw, s2 = rotate_refresh_token(db_session, raw)
    # rotated token inherits the family's absolute expiry (no sliding extension)
    assert s2.expires_at == original_expiry


def test_replay_within_grace_issues_sibling_not_theft(db_session):
    u = _user(db_session)
    raw, _s1 = issue_refresh_token(db_session, user_id=u.id)
    new_raw, s2 = rotate_refresh_token(db_session, raw)
    # Immediately replay the just-rotated token (age ~0 < grace) -> benign retry.
    sib_raw, sib = rotate_refresh_token(db_session, raw)
    assert sib_raw not in (raw, new_raw)
    assert sib.family_id == s2.family_id
    # the legitimate latest token is still active (family NOT revoked)
    assert session_dao.lookup_by_token(db_session, new_raw) is not None


def test_replay_beyond_grace_revokes_family_and_raises(db_session):
    u = _user(db_session)
    raw, s1 = issue_refresh_token(db_session, user_id=u.id)
    new_raw, _s2 = rotate_refresh_token(db_session, raw)
    # Age the old row's revocation past the grace window.
    old = session_dao.get_by_token_any_state(db_session, raw)
    old.revoked_at = datetime.now(timezone.utc) - timedelta(
        seconds=settings.REFRESH_ROTATION_GRACE_SECONDS + 30
    )
    db_session.flush()

    with pytest.raises(RefreshTokenReuse):
        rotate_refresh_token(db_session, raw)
    # the whole family is now revoked — the legit latest token is dead too
    assert session_dao.lookup_by_token(db_session, new_raw) is None


def test_expired_token_raises_invalid(db_session):
    u = _user(db_session)
    raw, s1 = issue_refresh_token(db_session, user_id=u.id)
    s1.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.flush()
    with pytest.raises(InvalidToken):
        rotate_refresh_token(db_session, raw)


def test_unknown_token_raises_invalid(db_session):
    with pytest.raises(InvalidToken):
        rotate_refresh_token(db_session, "never-issued-token")


def test_explicit_logout_then_rotate_is_plain_invalid_not_reuse(db_session):
    # A logged-out (explicitly revoked, replaced_by NULL) token replayed must be
    # a plain InvalidToken — NOT a RefreshTokenReuse theft alarm.
    u = _user(db_session)
    raw, _s1 = issue_refresh_token(db_session, user_id=u.id)
    revoke_refresh_token(db_session, raw)
    with pytest.raises(InvalidToken) as ei:
        rotate_refresh_token(db_session, raw)
    assert not isinstance(ei.value, RefreshTokenReuse)


def test_api_refresh_access_token_rotates_and_returns_new_token(db_session):
    u = _user(db_session)
    raw, _s1 = issue_refresh_token(db_session, user_id=u.id)
    out = identity_api.refresh_access_token(db_session, refresh_token=raw)
    assert set(out) == {"access_token", "refresh_token"}
    assert out["refresh_token"] != raw
    assert out["access_token"]  # a JWT was minted


def test_api_refresh_audits_replay_on_reuse(db_session):
    u = _user(db_session)
    raw, _s1 = issue_refresh_token(db_session, user_id=u.id)
    identity_api.refresh_access_token(db_session, refresh_token=raw)  # rotate once
    old = session_dao.get_by_token_any_state(db_session, raw)
    old.revoked_at = datetime.now(timezone.utc) - timedelta(
        seconds=settings.REFRESH_ROTATION_GRACE_SECONDS + 30
    )
    db_session.flush()
    with pytest.raises(RefreshTokenReuse):
        identity_api.refresh_access_token(db_session, refresh_token=raw)
    events = [a.event_type for a in db_session.query(AuditLog).all()]
    assert "auth.refresh_replay" in events
