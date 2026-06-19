from unittest.mock import patch

import pytest

from data_access.daos import email_otp_dao, user_dao
from data_access.models import AuditLog
from identity import api as identity_api
from identity.errors import RateLimited


def test_start_email_otp_shape_and_canonicalization(db_session):
    with patch("identity.api.deliver_email_otp", return_value=("email", "resend-1")):
        out = identity_api.start_email_otp(db_session, email="  USER@Example.COM ", ip_address="203.0.113.5")
    assert out["channel"] == "email"
    assert "otp_id" in out
    o = email_otp_dao.get_by_id(db_session, __import__("uuid").UUID(out["otp_id"]))
    assert o.email == "user@example.com"  # lowercased + stripped
    assert o.delivery_status == "delivered"
    assert "email_otp.issued" in [a.event_type for a in db_session.query(AuditLog).all()]


def test_start_email_otp_anti_enumeration_identical_for_unknown(db_session):
    # No user exists for this email; shape must be identical to a registered one.
    with patch("identity.api.deliver_email_otp", return_value=("email", "x")):
        out = identity_api.start_email_otp(db_session, email="ghost@example.com")
    assert set(out) >= {"otp_id", "channel"}
    assert out["channel"] == "email"


def test_start_email_otp_soft_fail_still_returns_otp_id(db_session):
    from identity.errors import EmailDeliveryFailed
    with patch("identity.api.deliver_email_otp", side_effect=EmailDeliveryFailed("resend 500")):
        out = identity_api.start_email_otp(db_session, email="fail@example.com")
    assert "otp_id" in out  # 200 anti-enumeration even on delivery soft-fail
    o = email_otp_dao.get_by_id(db_session, __import__("uuid").UUID(out["otp_id"]))
    assert o.delivery_status == "failed"


def test_start_email_otp_rate_limited(db_session):
    with patch("identity.api.deliver_email_otp", return_value=("email", "x")):
        for _ in range(5):  # OTP_PER_EMAIL_PER_HOUR default = 5
            identity_api.start_email_otp(db_session, email="rate@example.com")
        with pytest.raises(RateLimited):
            identity_api.start_email_otp(db_session, email="rate@example.com")


# ---------------------------------------------------------------------------
# P2.13 — verify_email_otp_and_login: D4 safeguarded resolution matrix
# ---------------------------------------------------------------------------

def _make_verified_email_otp(session, email):
    from identity.api import _canonicalize_email
    from identity.otp.issuer import hash_otp_code
    return email_otp_dao.insert(session, email=_canonicalize_email(email), code_hash=hash_otp_code("424242"))


def test_verify_email_new_email_creates_and_mints(db_session):
    o = _make_verified_email_otp(db_session, "new@example.com")
    db_session.flush()
    out = identity_api.verify_email_otp_and_login(
        db_session, otp_id=o.id, code="424242", brand="nowlez", name="N"
    )
    assert set(out) == {"access_token", "refresh_token", "user"}
    u = user_dao.get_by_email(db_session, "new@example.com")
    assert u is not None
    assert user_dao.is_email_verified(db_session, u.id) is True


def test_verify_email_already_verified_account_mints(db_session):
    u, _ = user_dao.get_or_create_by_email(db_session, email="known@example.com")
    user_dao.ensure_nowlez_extension(db_session, u.id, name="K")
    user_dao.set_email_verified(db_session, u.id)
    db_session.flush()
    o = _make_verified_email_otp(db_session, "known@example.com")
    db_session.flush()
    out = identity_api.verify_email_otp_and_login(db_session, otp_id=o.id, code="424242", brand="nowlez")
    assert out["user"]["id"] == str(u.id)


def test_verify_email_unverified_on_phone_account_requires_step_up(db_session):
    from identity.errors import AccountLinkStepUpRequired
    # account has a phone + password (a second factor exists) and an UNVERIFIED email
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    u.email = "linkme@example.com"
    u.password_hash = "$argon2id$set"
    user_dao.ensure_nowlez_extension(db_session, u.id, name="P")
    db_session.flush()
    o = _make_verified_email_otp(db_session, "linkme@example.com")
    db_session.flush()
    with pytest.raises(AccountLinkStepUpRequired):
        identity_api.verify_email_otp_and_login(db_session, otp_id=o.id, code="424242", brand="nowlez")


def test_verify_email_only_legacy_no_phone_no_password_mints(db_session):
    # email-only legacy row: no phone, no password -> email control is the only
    # anchor and is sufficient (D4 §8.1 case 2 second bullet).
    u, _ = user_dao.get_or_create_by_email(db_session, email="legacy@example.com")
    user_dao.ensure_nowlez_extension(db_session, u.id, name="L")
    db_session.flush()  # email_verified defaults False
    o = _make_verified_email_otp(db_session, "legacy@example.com")
    db_session.flush()
    out = identity_api.verify_email_otp_and_login(db_session, otp_id=o.id, code="424242", brand="nowlez")
    assert out["user"]["id"] == str(u.id)


def test_verify_email_wrong_code_does_not_mint(db_session):
    from identity.errors import OtpInvalid
    o = _make_verified_email_otp(db_session, "wrong@example.com")
    db_session.flush()
    with pytest.raises(OtpInvalid):
        identity_api.verify_email_otp_and_login(db_session, otp_id=o.id, code="000000", brand="nowlez")


def test_verify_email_replay_rejected(db_session):
    from identity.errors import OtpAlreadyUsed
    o = _make_verified_email_otp(db_session, "replay@example.com")
    db_session.flush()
    identity_api.verify_email_otp_and_login(db_session, otp_id=o.id, code="424242", brand="nowlez")
    with pytest.raises(OtpAlreadyUsed):
        identity_api.verify_email_otp_and_login(db_session, otp_id=o.id, code="424242", brand="nowlez")
