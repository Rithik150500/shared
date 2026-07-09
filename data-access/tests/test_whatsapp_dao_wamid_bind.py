"""Deterministic wamid binding in set_meta_message_id.

Regression cover for the prod bug where the daily-digest delivery rows (created
by _claim_daily_send_slot, keyed on user_id/template_name/send_date_ist) never
received their meta_message_id because the bind only keyed on rq_job_id, which
the live two-worker topology never resolved — so Meta status receipts (matched
on meta_message_id) could never update the row.
"""
from __future__ import annotations

from datetime import date

from data_access.daos import user_dao, whatsapp_dao
from data_access.models import WhatsAppDeliveryLog


def _user(s, phone="+919999999999"):
    u, _ = user_dao.get_or_create_by_phone(s, phone=phone)
    s.commit()
    return u


def test_binds_by_rq_job_id(db_session):
    u = _user(db_session)
    whatsapp_dao.log_send_enqueued(
        db_session, user_id=u.id, template_name="t_v1", brand="nowlez", rq_job_id="job-1"
    )
    n = whatsapp_dao.set_meta_message_id(
        db_session, rq_job_id="job-1", meta_message_id="wamid.A"
    )
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(rq_job_id="job-1").one()
    assert row.meta_message_id == "wamid.A"


def test_binds_daily_row_by_natural_key_when_rq_job_id_misses(db_session):
    # A daily-claim row keyed by (user, template, send_date_ist); the bind's
    # rq_job_id does NOT resolve it (the live-topology failure). The natural key
    # must still land the wamid, and NOT create a duplicate row.
    u = _user(db_session)
    today = date(2026, 7, 9)
    db_session.add(
        WhatsAppDeliveryLog(
            user_id=u.id,
            template_name="nowlez_tomorrow_hearings_v1",
            brand="nowlez",
            rq_job_id="claim-job",
            send_date_ist=today,
            delivery_status="pending",
        )
    )
    db_session.commit()

    n = whatsapp_dao.set_meta_message_id(
        db_session,
        rq_job_id="a-different-unresolvable-job",
        meta_message_id="wamid.B",
        user_id=u.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=today,
    )
    assert n == 1
    rows = db_session.query(WhatsAppDeliveryLog).all()
    assert len(rows) == 1  # no duplicate inserted
    assert rows[0].meta_message_id == "wamid.B"


def test_inserts_row_when_none_exists(db_session):
    # Transactional sends (signup welcome, OTP, billing) create no row today.
    u = _user(db_session)
    n = whatsapp_dao.set_meta_message_id(
        db_session,
        rq_job_id=None,
        meta_message_id="wamid.C",
        user_id=u.id,
        template_name="nowlez_signup_welcome_v2",
        brand="nowlez",
    )
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(meta_message_id="wamid.C").one()
    assert row.delivery_status == "sent"
    assert row.template_name == "nowlez_signup_welcome_v2"


def test_never_overwrites_existing_wamid(db_session):
    u = _user(db_session)
    whatsapp_dao.log_send_enqueued(
        db_session, user_id=u.id, template_name="t_v1", brand="nowlez", rq_job_id="job-x"
    )
    whatsapp_dao.set_meta_message_id(
        db_session, rq_job_id="job-x", meta_message_id="wamid.first"
    )
    # Re-bind attempt with the same rq_job_id but a different wamid must be a no-op.
    n = whatsapp_dao.set_meta_message_id(
        db_session, rq_job_id="job-x", meta_message_id="wamid.second"
    )
    assert n == 0
    row = db_session.query(WhatsAppDeliveryLog).filter_by(rq_job_id="job-x").one()
    assert row.meta_message_id == "wamid.first"
