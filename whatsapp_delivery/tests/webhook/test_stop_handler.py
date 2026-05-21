"""Tests for whatsapp_delivery.webhook.stop_handler.

The handler must:
  - flip both users_nowlez consent flags (events + reminders) to False
  - write an audit_log row with event_type='whatsapp.opted_out_via_inbound'
  - enqueue a confirmation template send to the user's phone

Uses the SQLite in-memory ``db_session`` fixture from data_access tests for
the DB-touching assertions and monkey-patches ``enqueue_send_template`` to
record the call (the actual RQ queue lands in Task 6 -- this test only
verifies the handler invokes the queue helper with the right shape).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa: F401 — register all models with metadata
from data_access.daos import user_dao
from data_access.models import AuditLog, UserNowlez
from whatsapp_delivery.webhook.parser import IncomingMessage
from whatsapp_delivery.webhook.stop_handler import handle_stop_keyword


@pytest.fixture
def db_session():
    """Per-test sqlite session that mirrors data_access's db_session shape."""
    import sqlite3
    sqlite3.register_adapter(uuid.UUID, str)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _msg(text: str = "STOP", phone: str = "+919876543210") -> IncomingMessage:
    return IncomingMessage(
        meta_message_id="wamid.stop.1",
        from_phone=phone,
        timestamp=1715817600,
        type="text",
        text=text,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_stop_handler_flips_both_consent_flags(db_session, monkeypatch):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    user_dao.ensure_nowlez_extension(db_session, user.id, name="N")
    db_session.commit()

    sent = {}

    async def fake_enqueue(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(
        "whatsapp_delivery.webhook.stop_handler.enqueue_send_template",
        fake_enqueue,
    )

    _run(handle_stop_keyword(user.id, _msg(), db_session))

    ext = db_session.get(UserNowlez, user.id)
    assert ext.whatsapp_events_enabled is False
    assert ext.whatsapp_reminders_enabled is False


def test_stop_handler_writes_audit_row(db_session, monkeypatch):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    user_dao.ensure_nowlez_extension(db_session, user.id, name="N")
    db_session.commit()

    async def fake_enqueue(**kwargs):
        pass

    monkeypatch.setattr(
        "whatsapp_delivery.webhook.stop_handler.enqueue_send_template",
        fake_enqueue,
    )

    _run(handle_stop_keyword(user.id, _msg("STOP"), db_session))

    rows = db_session.query(AuditLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "whatsapp.opted_out_via_inbound"
    assert row.source == "nowlez"
    assert str(row.user_id) == str(user.id)
    # metadata captures both the inbound wamid and the trimmed text + phone.
    md = row.metadata_
    assert md["meta_message_id"] == "wamid.stop.1"
    assert md["phone"] == "+919876543210"
    assert "STOP" in md["inbound_text"].upper()


def test_stop_handler_enqueues_confirmation_template(db_session, monkeypatch):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    user_dao.ensure_nowlez_extension(db_session, user.id, name="N")
    db_session.commit()

    sent: dict = {}

    async def fake_enqueue(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(
        "whatsapp_delivery.webhook.stop_handler.enqueue_send_template",
        fake_enqueue,
    )

    _run(handle_stop_keyword(user.id, _msg(), db_session))

    assert sent["to"] == "+919876543210"
    assert sent["template_name"] == "nowlez_stop_confirmation_v1"
    assert sent["brand"] == "nowlez"
    assert sent["language"] == "en_US"
    assert sent["variables"] == {}


def test_stop_handler_truncates_long_inbound_text_in_audit(db_session, monkeypatch):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    user_dao.ensure_nowlez_extension(db_session, user.id, name="N")
    db_session.commit()

    async def fake_enqueue(**kwargs):
        pass

    monkeypatch.setattr(
        "whatsapp_delivery.webhook.stop_handler.enqueue_send_template",
        fake_enqueue,
    )

    long_text = "STOP " + ("x" * 500)
    _run(handle_stop_keyword(user.id, _msg(long_text), db_session))

    row = db_session.query(AuditLog).one()
    # Cap at 100 chars per the spec to keep audit-log payloads sane.
    assert len(row.metadata_["inbound_text"]) == 100


def test_stop_handler_does_not_revoke_consent_when_enqueue_fails(db_session, monkeypatch):
    """D-10 fix: enqueue must run BEFORE the DB commit so a producer crash
    between commit and enqueue can't leave the user opted-out with no
    confirmation message. We simulate the failure by raising from
    ``enqueue_send_template`` and asserting:
        * the consent flags are still True (no revocation persisted)
        * no audit row was written

    The user retries STOP later; the handler re-processes cleanly and they
    end up correctly opted-out (acceptable failure mode per the audit note).
    The unacceptable current behavior — consent revoked, no confirmation,
    user thinks the bot is dead — is what this test guards against.
    """
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    user_dao.ensure_nowlez_extension(db_session, user.id, name="N")
    db_session.commit()

    # Sanity: pre-state has both flags True (the default established by
    # ensure_nowlez_extension).
    ext_before = db_session.get(UserNowlez, user.id)
    assert ext_before.whatsapp_events_enabled is True
    assert ext_before.whatsapp_reminders_enabled is True

    class _EnqueueBoom(RuntimeError):
        pass

    async def fake_enqueue(**kwargs):
        raise _EnqueueBoom("simulated queue/producer failure")

    monkeypatch.setattr(
        "whatsapp_delivery.webhook.stop_handler.enqueue_send_template",
        fake_enqueue,
    )

    with pytest.raises(_EnqueueBoom):
        _run(handle_stop_keyword(user.id, _msg(), db_session))

    # The handler raised; the DB transaction must be rolled back so the
    # consent flags are unchanged and no audit row was committed. We open a
    # fresh session against the same engine to bypass the original session's
    # identity-map cache (which could mask an uncommitted in-memory mutation).
    engine = db_session.get_bind()
    from sqlalchemy.orm import sessionmaker as _sm
    fresh = _sm(bind=engine, future=True)()
    try:
        ext_after = fresh.get(UserNowlez, user.id)
        assert ext_after.whatsapp_events_enabled is True, (
            "consent must NOT be revoked when enqueue fails — D-10 regression"
        )
        assert ext_after.whatsapp_reminders_enabled is True
        assert fresh.query(AuditLog).count() == 0, (
            "no audit row should be persisted when the enqueue step fails"
        )
    finally:
        fresh.close()
