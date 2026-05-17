"""Tests for whatsapp_dao — idempotency + delivery-log + consent helpers.

Uses the SQLite ``db_session`` fixture for the common-path tests; the DAO's
``claim_message`` uses dialect-specific ON CONFLICT but covers both Postgres
and SQLite via the ``insert as pg_insert``/``insert as sqlite_insert`` branch.
"""
from __future__ import annotations

import uuid

import pytest

from data_access.daos import user_dao, whatsapp_dao
from data_access.models import MessageLog, User, UserNowlez, WhatsAppDeliveryLog


def _make_user(s, phone: str = "+919999999999") -> User:
    user, _ = user_dao.get_or_create_by_phone(s, phone=phone)
    s.commit()
    return user


def test_claim_message_first_sighting_returns_true(db_session):
    assert whatsapp_dao.claim_message(
        db_session,
        meta_message_id="wamid.1",
        user_phone="+919876543210",
    ) is True
    # Row was inserted.
    row = db_session.query(MessageLog).filter_by(meta_message_id="wamid.1").one()
    assert row.user_phone == "+919876543210"


def test_claim_message_retry_returns_false(db_session):
    # First sighting.
    assert whatsapp_dao.claim_message(
        db_session, meta_message_id="wamid.2", user_phone="+919876543210"
    ) is True
    # Retry — same meta_message_id, must not raise; returns False.
    assert whatsapp_dao.claim_message(
        db_session, meta_message_id="wamid.2", user_phone="+919876543210"
    ) is False
    # Still exactly one row.
    rows = db_session.query(MessageLog).filter_by(meta_message_id="wamid.2").all()
    assert len(rows) == 1


def test_log_send_enqueued_returns_uuid_id(db_session):
    user = _make_user(db_session)
    log_id = whatsapp_dao.log_send_enqueued(
        db_session,
        user_id=user.id,
        template_name="nowlez_order_uploaded_v1",
        brand="nowlez",
        rq_job_id="rq.job.abc",
    )
    # The id is either UUID (Postgres) or string (SQLite with_variant).
    assert log_id is not None
    row = db_session.get(WhatsAppDeliveryLog, log_id)
    assert str(row.user_id) == str(user.id)
    assert row.template_name == "nowlez_order_uploaded_v1"
    assert row.brand == "nowlez"
    assert row.rq_job_id == "rq.job.abc"
    assert row.delivery_status == "pending"


def test_set_meta_message_id_links_rq_job_to_meta(db_session):
    user = _make_user(db_session)
    log_id = whatsapp_dao.log_send_enqueued(
        db_session,
        user_id=user.id,
        template_name="t1",
        brand="munshi",
        rq_job_id="rq.job.xyz",
    )
    n = whatsapp_dao.set_meta_message_id(
        db_session, rq_job_id="rq.job.xyz", meta_message_id="wamid.outbound.1",
    )
    assert n == 1
    row = db_session.get(WhatsAppDeliveryLog, log_id)
    assert row.meta_message_id == "wamid.outbound.1"


def test_update_delivery_status_sets_sent_timestamp(db_session):
    user = _make_user(db_session)
    whatsapp_dao.log_send_enqueued(
        db_session, user_id=user.id, template_name="t1", brand="munshi",
        rq_job_id="rq.j",
    )
    whatsapp_dao.set_meta_message_id(
        db_session, rq_job_id="rq.j", meta_message_id="wamid.s",
    )
    n = whatsapp_dao.update_delivery_status(
        db_session, meta_message_id="wamid.s", status="sent", timestamp=1715817600,
    )
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.s"
    ).one()
    assert row.delivery_status == "sent"
    assert row.sent_at is not None


def test_update_delivery_status_failed_sets_reason(db_session):
    user = _make_user(db_session)
    whatsapp_dao.log_send_enqueued(
        db_session, user_id=user.id, template_name="t1", brand="munshi",
        rq_job_id="rq.f",
    )
    whatsapp_dao.set_meta_message_id(
        db_session, rq_job_id="rq.f", meta_message_id="wamid.f",
    )
    n = whatsapp_dao.update_delivery_status(
        db_session, meta_message_id="wamid.f", status="failed",
        timestamp=1715817700, failure_reason="Re-engagement window",
    )
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.f"
    ).one()
    assert row.delivery_status == "failed"
    assert row.failure_reason == "Re-engagement window"


def test_get_users_with_whatsapp_events_enabled(db_session):
    user_on, _ = user_dao.get_or_create_by_phone(db_session, phone="+91111111111")
    user_off, _ = user_dao.get_or_create_by_phone(db_session, phone="+91222222222")
    user_no_phone, _ = user_dao.get_or_create_by_phone(db_session, phone="+91333333333")
    db_session.commit()

    user_dao.ensure_nowlez_extension(db_session, user_on.id, name="On")
    ext_off = user_dao.ensure_nowlez_extension(db_session, user_off.id, name="Off")
    ext_off.whatsapp_events_enabled = False
    user_dao.ensure_nowlez_extension(db_session, user_no_phone.id, name="Mute")
    # Strip phone on the third user so the filter excludes it.
    user_no_phone.phone = None
    db_session.commit()

    enabled = list(whatsapp_dao.get_users_with_whatsapp_events_enabled(db_session))
    enabled_ids = {str(u.id) for u in enabled}
    assert str(user_on.id) in enabled_ids
    assert str(user_off.id) not in enabled_ids
    assert str(user_no_phone.id) not in enabled_ids


def test_get_users_with_whatsapp_reminders_enabled(db_session):
    user_on, _ = user_dao.get_or_create_by_phone(db_session, phone="+91444444444")
    user_off, _ = user_dao.get_or_create_by_phone(db_session, phone="+91555555555")
    db_session.commit()

    user_dao.ensure_nowlez_extension(db_session, user_on.id, name="On")
    ext_off = user_dao.ensure_nowlez_extension(db_session, user_off.id, name="Off")
    ext_off.whatsapp_reminders_enabled = False
    db_session.commit()

    enabled = list(whatsapp_dao.get_users_with_whatsapp_reminders_enabled(db_session))
    enabled_ids = {str(u.id) for u in enabled}
    assert str(user_on.id) in enabled_ids
    assert str(user_off.id) not in enabled_ids


def test_update_nowlez_preferences_flips_both(db_session):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    user_dao.ensure_nowlez_extension(db_session, user.id, name="N")
    db_session.commit()

    whatsapp_dao.update_nowlez_preferences(
        db_session,
        user_id=user.id,
        whatsapp_events_enabled=False,
        whatsapp_reminders_enabled=False,
    )

    ext = db_session.get(UserNowlez, user.id)
    assert ext.whatsapp_events_enabled is False
    assert ext.whatsapp_reminders_enabled is False
