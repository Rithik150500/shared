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
