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


# ---------------------------------------------------------------------------
# D4 §8.1 branch 4: second_signal_user_id gate (adversarial)
# ---------------------------------------------------------------------------

def test_d4_matching_second_signal_mints(db_session):
    """Branch 4: unverified email on phone+password account, same-flow
    second_signal_user_id matches that account's id -> must mint (link)."""
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919000000001")
    u.email = "d4match@example.com"
    u.password_hash = "$argon2id$v=19$set"
    user_dao.ensure_nowlez_extension(db_session, u.id, name="D4")
    db_session.flush()
    o = _make_verified_email_otp(db_session, "d4match@example.com")
    db_session.flush()
    out = identity_api.verify_email_otp_and_login(
        db_session, otp_id=o.id, code="424242", brand="nowlez",
        second_signal_user_id=u.id,
    )
    assert set(out) == {"access_token", "refresh_token", "user"}
    assert out["user"]["id"] == str(u.id)
    assert user_dao.is_email_verified(db_session, u.id) is True


def test_d4_mismatched_second_signal_raises_not_mints(db_session):
    """Branch 5 guard: second_signal_user_id that does NOT match the resolved
    account must raise AccountLinkStepUpRequired, never mint. An attacker who
    controls a different phone account cannot leverage it to link a third
    account's email."""
    from identity.errors import AccountLinkStepUpRequired
    import uuid as _uuid

    # The account whose email is being targeted (has phone -> second factor)
    victim, _ = user_dao.get_or_create_by_phone(db_session, phone="+919000000002")
    victim.email = "d4victim@example.com"
    victim.password_hash = "$argon2id$v=19$set"
    user_dao.ensure_nowlez_extension(db_session, victim.id, name="V")
    db_session.flush()

    # A different account the attacker happens to control
    attacker, _ = user_dao.get_or_create_by_phone(db_session, phone="+919000000003")
    user_dao.ensure_nowlez_extension(db_session, attacker.id, name="A")
    db_session.flush()

    o = _make_verified_email_otp(db_session, "d4victim@example.com")
    db_session.flush()
    with pytest.raises(AccountLinkStepUpRequired):
        identity_api.verify_email_otp_and_login(
            db_session, otp_id=o.id, code="424242", brand="nowlez",
            second_signal_user_id=attacker.id,  # mismatched: different user's id
        )


# ---------------------------------------------------------------------------
# P2.14 — link_email_to_phone_account: D4 merge + conflict refusal
# ---------------------------------------------------------------------------

def test_link_merges_when_both_identifiers_proven(db_session):
    from data_access.models import AuditLog
    # phone-only account (older), email-only account (newer) = same human
    survivor, _ = user_dao.get_or_create_by_phone(db_session, phone="+919800000010")
    user_dao.ensure_nowlez_extension(db_session, survivor.id, name="Survivor")
    db_session.flush()
    absorbed, _ = user_dao.get_or_create_by_email(db_session, email="both@example.com")
    user_dao.ensure_nowlez_extension(db_session, absorbed.id, name="Absorbed")
    db_session.flush()
    out = identity_api.link_email_to_phone_account(
        db_session, phone_user_id=survivor.id, email_user_id=absorbed.id
    )
    assert out is True
    assert user_dao.get_by_id(db_session, absorbed.id) is None
    assert "account.merged" in [a.event_type for a in db_session.query(AuditLog).all()]


def test_link_refuses_merge_when_both_own_distinct_phones(db_session):
    from data_access.models import AuditLog
    a, _ = user_dao.get_or_create_by_phone(db_session, phone="+919800000020")
    b, _ = user_dao.get_or_create_by_phone(db_session, phone="+919800000021")
    db_session.flush()
    out = identity_api.link_email_to_phone_account(
        db_session, phone_user_id=a.id, email_user_id=b.id
    )
    assert out is False
    # both rows survive; conflict audited
    assert user_dao.get_by_id(db_session, a.id) is not None
    assert user_dao.get_by_id(db_session, b.id) is not None
    assert "account.merge_conflict" in [x.event_type for x in db_session.query(AuditLog).all()]


def test_link_catches_merge_unsafe_error_and_returns_false(db_session):
    """MergeUnsafeError (absorbed owns munshi/cases) must NOT propagate —
    link_email_to_phone_account catches it, audits account.merge_conflict, returns False."""
    from data_access.models import AuditLog, UserMunshi
    # Older phone account (will be survivor by created_at)
    survivor, _ = user_dao.get_or_create_by_phone(db_session, phone="+919800000030")
    user_dao.ensure_nowlez_extension(db_session, survivor.id, name="Survivor2")
    db_session.flush()
    # Absorbed: email-only row that has a UserMunshi record → merge_users raises MergeUnsafeError.
    # Insert UserMunshi directly so we skip ensure_munshi_extension's phone-guard.
    absorbed, _ = user_dao.get_or_create_by_email(db_session, email="unsafe@example.com")
    user_dao.ensure_nowlez_extension(db_session, absorbed.id, name="Absorbed2")
    db_session.add(UserMunshi(user_id=absorbed.id))
    db_session.flush()
    out = identity_api.link_email_to_phone_account(
        db_session, phone_user_id=survivor.id, email_user_id=absorbed.id
    )
    assert out is False
    events = [a.event_type for a in db_session.query(AuditLog).all()]
    assert "account.merge_conflict" in events
    # absorbed row MUST still exist (no cascade destroy)
    assert user_dao.get_by_id(db_session, absorbed.id) is not None
