from __future__ import annotations

import uuid

import pytest

from identity import api as identity_api
from data_access.daos import user_dao
from data_access.models import AuditLog


def test_notify_sends_security_email_and_audits(db_session, monkeypatch):
    sent = {}
    def fake_send(to_email, subject, body, **kw):
        sent["to"] = to_email; sent["subject"] = subject
        return "email-prov-1"
    monkeypatch.setattr(identity_api, "send_security_email", fake_send)

    u, _ = user_dao.get_or_create_by_email(db_session, email="alert@example.com")
    db_session.flush()
    identity_api._notify_account_security(db_session, u, event="email_login_new_device")

    assert sent["to"] == "alert@example.com"
    assert "email_login_new_device" in sent["subject"] or "sign-in" in sent["subject"].lower()
    assert "account.security_alert_sent" in [a.event_type for a in db_session.query(AuditLog).all()]


def test_notify_is_best_effort_on_send_failure(db_session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("resend down")
    monkeypatch.setattr(identity_api, "send_security_email", boom)

    u, _ = user_dao.get_or_create_by_email(db_session, email="alert2@example.com")
    db_session.flush()
    # MUST NOT raise — best-effort
    identity_api._notify_account_security(db_session, u, event="account_merged")
    assert "account.security_alert_failed" in [a.event_type for a in db_session.query(AuditLog).all()]


def test_notify_noop_when_user_has_no_email(db_session, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(identity_api, "send_security_email", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919811111111")
    db_session.flush()
    identity_api._notify_account_security(db_session, u, event="email_login_new_device")
    assert called["n"] == 0  # nothing to email


# ---------------------------------------------------------------------------
# Ordering fix: ensure_nowlez_extension before set_email_verified in branch 4
# (phone-seeded nowlez user linking email via second_signal_user_id)
# ---------------------------------------------------------------------------

def test_branch4_email_verified_persists_for_phone_seeded_nowlez_user(db_session, monkeypatch):
    """Branch 4 ordering fix: ensure_nowlez_extension is called BEFORE
    set_email_verified so that is_email_verified returns True after the link."""
    from unittest.mock import patch
    from data_access.daos import email_otp_dao
    from identity.api import _canonicalize_email
    from identity.otp.issuer import hash_otp_code

    # Silence the security alert HTTP call
    monkeypatch.setattr(identity_api, "send_security_email", lambda *a, **kw: "no-op")

    # Phone-seeded user with an unverified email (typical phone-first signup)
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919822222222")
    u.email = "branch4fix@example.com"
    u.password_hash = "$argon2id$v=19$set"
    # No nowlez extension yet (phone-only signup; nowlez ext created at first web login)
    db_session.flush()

    o = email_otp_dao.insert(
        db_session,
        email=_canonicalize_email("branch4fix@example.com"),
        code_hash=hash_otp_code("424242"),
    )
    db_session.flush()

    with patch("identity.api.deliver_email_otp", return_value=("email", "x")):
        out = identity_api.verify_email_otp_and_login(
            db_session, otp_id=o.id, code="424242", brand="nowlez", name="Branch4",
            second_signal_user_id=u.id,
        )

    assert out["user"]["id"] == str(u.id)
    # email_verified must persist (the ordering fix ensures the nowlez extension
    # exists before set_email_verified is called)
    assert user_dao.is_email_verified(db_session, u.id) is True
