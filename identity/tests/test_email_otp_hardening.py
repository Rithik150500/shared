"""Regression tests for the email-OTP audit fixes.

#3 one-live-code: a re-request supersedes prior unused codes (so the per-code
   attempt budget can't be reset by re-requesting).
#7 expiry-before-hash: an expired code raises OtpExpired without running argon2.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from data_access.daos import email_otp_dao
from identity import api as identity_api
from identity.errors import OtpAlreadyUsed, OtpExpired
from identity.otp.issuer import hash_otp_code


def _issue(session, email="hard@example.com"):
    with patch("identity.api.deliver_email_otp", return_value=("email", "x")):
        return identity_api.start_email_otp(session, email=email)


def test_new_request_supersedes_prior_unused_code(db_session):
    import uuid

    first = _issue(db_session)
    first_id = uuid.UUID(first["otp_id"])
    # The first code is live right after issue.
    assert email_otp_dao.get_by_id(db_session, first_id).used_at is None

    # A second request must supersede the first (one-live-code).
    second = _issue(db_session)
    assert second["otp_id"] != first["otp_id"]
    assert email_otp_dao.get_by_id(db_session, first_id).used_at is not None
    # The newest code remains live.
    assert email_otp_dao.get_by_id(db_session, uuid.UUID(second["otp_id"])).used_at is None


def test_superseded_code_cannot_be_verified(db_session):
    first = _issue(db_session, email="supersede@example.com")
    _issue(db_session, email="supersede@example.com")  # supersedes the first
    db_session.flush()
    # Verifying the (now superseded) first code must fail as already-used,
    # NOT mint a session.
    with pytest.raises(OtpAlreadyUsed):
        identity_api.verify_email_otp_and_login(
            db_session, otp_id=first["otp_id"], code="424242", brand="nowlez"
        )


def test_expired_code_raises_before_argon2(db_session):
    # Insert a code that is already expired.
    o = email_otp_dao.insert(
        db_session, email="expired@example.com", code_hash=hash_otp_code("424242")
    )
    o.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.flush()

    # The expiry pre-check (added in the audit fix) raises OtpExpired cheaply,
    # BEFORE the argon2 verify. The old code path reached mark_used and surfaced
    # an expired code as OtpAlreadyUsed instead, so asserting OtpExpired pins the
    # new behaviour.
    with pytest.raises(OtpExpired):
        identity_api.verify_email_otp_and_login(
            db_session, otp_id=o.id, code="424242", brand="nowlez"
        )
