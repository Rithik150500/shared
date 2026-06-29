"""verify_google_id_token_and_login: token gating, account resolution, sub link.

Mirrors test_email_otp_api.py. The network boundary (Google cert fetch +
signature check) is stubbed by patching identity.api.verify_google_id_token to
return claims; GOOGLE_OAUTH_CLIENT_ID is set so the audience guard passes.
"""
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from data_access.daos import user_dao
from data_access.models import AuditLog, UserExternalIdentity
from identity import api as identity_api
from identity.errors import AccountLinkStepUpRequired, GoogleTokenInvalid


@contextmanager
def _google(claims, *, client_id="web-client-123"):
    """Configure a client id and stub the verified Google claims."""
    with patch.object(identity_api.settings, "GOOGLE_OAUTH_CLIENT_ID", client_id), \
            patch("identity.api.verify_google_id_token", return_value=claims):
        yield


def _claims(email, sub="google-sub-1", *, email_verified=True, name="G User"):
    return {"sub": sub, "email": email, "email_verified": email_verified, "name": name}


def test_google_new_email_creates_links_and_mints(db_session):
    with _google(_claims("new@example.com")):
        out = identity_api.verify_google_id_token_and_login(
            db_session, id_token="tok", brand="nowlez"
        )
    assert set(out) == {"access_token", "refresh_token", "user"}
    u = user_dao.get_by_email(db_session, "new@example.com")
    assert u is not None
    assert user_dao.is_email_verified(db_session, u.id) is True
    # sub link persisted
    assert user_dao.get_by_google_sub(db_session, "google-sub-1").id == u.id
    events = [a.event_type for a in db_session.query(AuditLog).all()]
    assert "user.created" in events
    # the Google identity link is audited (observability of first-time link)
    assert "account.google_linked" in events


def test_google_auto_links_existing_verified_email_account(db_session):
    # Pre-existing verified-email account; Google login on the same email must
    # auto-link (user's choice) and resolve to that SAME account.
    u, _ = user_dao.get_or_create_by_email(db_session, email="known@example.com")
    user_dao.ensure_nowlez_extension(db_session, u.id, name="K")
    user_dao.set_email_verified(db_session, u.id)
    db_session.flush()
    with _google(_claims("known@example.com", sub="sub-known")):
        out = identity_api.verify_google_id_token_and_login(
            db_session, id_token="tok", brand="nowlez"
        )
    assert out["user"]["id"] == str(u.id)
    assert user_dao.get_by_google_sub(db_session, "sub-known").id == u.id


def test_google_sub_is_authoritative_over_changed_email(db_session):
    # First login establishes the sub link.
    with _google(_claims("orig@example.com", sub="stable-sub")):
        first = identity_api.verify_google_id_token_and_login(
            db_session, id_token="tok", brand="nowlez"
        )
    # Same sub, but Google now reports a DIFFERENT email -> must resolve to the
    # SAME user via the stable sub anchor, not create a new account.
    with _google(_claims("changed@example.com", sub="stable-sub")):
        second = identity_api.verify_google_id_token_and_login(
            db_session, id_token="tok2", brand="nowlez"
        )
    assert second["user"]["id"] == first["user"]["id"]
    # No second account was created for the new email.
    assert user_dao.get_by_email(db_session, "changed@example.com") is None


def test_google_unverified_email_rejected(db_session):
    with _google(_claims("unverified@example.com", email_verified=False)):
        with pytest.raises(GoogleTokenInvalid):
            identity_api.verify_google_id_token_and_login(
                db_session, id_token="tok", brand="nowlez"
            )


def test_google_email_verified_string_true_accepted(db_session):
    # Google sometimes serialises email_verified as the string "true".
    with _google(_claims("strbool@example.com", email_verified="true")):
        out = identity_api.verify_google_id_token_and_login(
            db_session, id_token="tok", brand="nowlez"
        )
    assert out["user"]["id"] == str(user_dao.get_by_email(db_session, "strbool@example.com").id)


def test_google_missing_email_rejected(db_session):
    with _google({"sub": "s", "email_verified": True}):
        with pytest.raises(GoogleTokenInvalid):
            identity_api.verify_google_id_token_and_login(
                db_session, id_token="tok", brand="nowlez"
            )


def test_google_unconfigured_client_id_rejected(db_session):
    with patch.object(identity_api.settings, "GOOGLE_OAUTH_CLIENT_ID", ""):
        with pytest.raises(GoogleTokenInvalid):
            identity_api.verify_google_id_token_and_login(
                db_session, id_token="tok", brand="nowlez"
            )


def test_google_unverified_email_on_second_factor_account_requires_step_up(db_session):
    # An account with a phone + password (second factor) and an UNVERIFIED email
    # must NOT silently auto-link a Google login on that email — D4 step-up.
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500011")
    u.email = "collide@example.com"
    u.password_hash = "$argon2id$set"
    user_dao.ensure_nowlez_extension(db_session, u.id, name="P")
    db_session.flush()
    with _google(_claims("collide@example.com", sub="sub-collide")):
        with pytest.raises(AccountLinkStepUpRequired):
            identity_api.verify_google_id_token_and_login(
                db_session, id_token="tok", brand="nowlez"
            )
    # No link should have been created on the refused path.
    assert user_dao.get_by_google_sub(db_session, "sub-collide") is None


def test_google_step_up_satisfied_by_matching_second_signal(db_session):
    # Same collision, but the caller proves control of that account (bearer)
    # via second_signal_user_id -> link allowed.
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500022")
    u.email = "linkok@example.com"
    u.password_hash = "$argon2id$set"
    user_dao.ensure_nowlez_extension(db_session, u.id, name="P2")
    db_session.flush()
    with _google(_claims("linkok@example.com", sub="sub-linkok")):
        out = identity_api.verify_google_id_token_and_login(
            db_session, id_token="tok", brand="nowlez", second_signal_user_id=u.id
        )
    assert out["user"]["id"] == str(u.id)
    assert user_dao.get_by_google_sub(db_session, "sub-linkok").id == u.id


def test_google_relogin_is_idempotent_link(db_session):
    # Two logins for the same sub must not create duplicate identity rows.
    with _google(_claims("idem@example.com", sub="sub-idem")):
        identity_api.verify_google_id_token_and_login(db_session, id_token="t1", brand="nowlez")
        identity_api.verify_google_id_token_and_login(db_session, id_token="t2", brand="nowlez")
    rows = db_session.query(UserExternalIdentity).filter_by(provider_sub="sub-idem").all()
    assert len(rows) == 1
