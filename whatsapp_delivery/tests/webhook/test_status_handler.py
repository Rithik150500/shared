"""Tests for whatsapp_delivery.webhook.status_handler.

The handler must:
  - Look up the ``whatsapp_delivery_log`` row by ``meta_message_id`` and
    UPDATE ``delivery_status`` + the matching timestamp column
    (``sent_at`` / ``delivered_at`` / ``read_at``) per receipt.
  - Persist ``failure_reason`` on ``failed`` receipts.
  - Tolerate receipts whose ``meta_message_id`` has no matching log row yet
    (race against the worker's wamid-bind step) — rowcount==0, no exception.
  - Fire a best-effort Sentry capture on ``failed`` receipts; the failure
    path must not raise when ``sentry_sdk`` isn't installed.

Mirrors the SQLite in-memory fixture pattern from ``test_stop_handler.py``
so the suite is hermetic and fast.
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa: F401 — register all models with metadata
from data_access.daos import user_dao, whatsapp_dao
from data_access.models import WhatsAppDeliveryLog

from whatsapp_delivery.webhook.parser import DeliveryStatus, parse_status_updates
from whatsapp_delivery.webhook.status_handler import (
    apply_status_update,
    apply_status_updates,
)


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def db_session():
    """Per-test SQLite session that mirrors the data_access shape."""
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


def _seed_log_row(db_session, *, meta_message_id: str) -> WhatsAppDeliveryLog:
    """Insert a ``pending`` delivery-log row tied to a fresh user, then bind the wamid."""
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.commit()
    log_id = whatsapp_dao.log_send_enqueued(
        db_session,
        user_id=user.id,
        template_name="t1",
        brand="nowlez",
        rq_job_id=f"rq.{meta_message_id}",
    )
    whatsapp_dao.set_meta_message_id(
        db_session,
        rq_job_id=f"rq.{meta_message_id}",
        meta_message_id=meta_message_id,
    )
    return db_session.get(WhatsAppDeliveryLog, log_id)


def _status(meta_message_id: str, status: str, ts: int = 1715817600,
            failure_reason: str | None = None) -> DeliveryStatus:
    return DeliveryStatus(
        meta_message_id=meta_message_id,
        recipient_id="919876543210",
        status=status,
        timestamp=ts,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# apply_status_update — one receipt at a time
# ---------------------------------------------------------------------------


def test_apply_status_update_sent_stamps_sent_at(db_session):
    _seed_log_row(db_session, meta_message_id="wamid.sent.1")
    n = apply_status_update(db_session, _status("wamid.sent.1", "sent", ts=1715817600))
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.sent.1",
    ).one()
    assert row.delivery_status == "sent"
    # SQLite returns the DateTime as a naive datetime, Postgres tz-aware; both
    # should equal the unix-ts moment we passed in.
    assert row.sent_at is not None
    assert int(row.sent_at.replace(tzinfo=row.sent_at.tzinfo or timezone.utc).timestamp()) == 1715817600
    assert row.delivered_at is None
    assert row.read_at is None


def test_apply_status_update_delivered_stamps_delivered_at(db_session):
    _seed_log_row(db_session, meta_message_id="wamid.deliv.1")
    n = apply_status_update(db_session, _status("wamid.deliv.1", "delivered", ts=1715817700))
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.deliv.1",
    ).one()
    assert row.delivery_status == "delivered"
    assert row.delivered_at is not None
    assert row.sent_at is None  # only the matching col is set


def test_apply_status_update_read_stamps_read_at(db_session):
    _seed_log_row(db_session, meta_message_id="wamid.read.1")
    n = apply_status_update(db_session, _status("wamid.read.1", "read", ts=1715817800))
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.read.1",
    ).one()
    assert row.delivery_status == "read"
    assert row.read_at is not None


def test_apply_status_update_failed_writes_failure_reason(db_session):
    _seed_log_row(db_session, meta_message_id="wamid.failed.1")
    n = apply_status_update(
        db_session,
        _status("wamid.failed.1", "failed", ts=1715817800,
                failure_reason="Re-engagement message"),
    )
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.failed.1",
    ).one()
    assert row.delivery_status == "failed"
    assert row.failure_reason == "Re-engagement message"
    # ``failed`` is not in the col_map so none of the timestamp cols are set.
    assert row.sent_at is None
    assert row.delivered_at is None
    assert row.read_at is None


def test_apply_status_update_no_row_yet_returns_zero(db_session):
    """Receipt races ahead of the wamid-bind: rowcount==0, no exception."""
    n = apply_status_update(
        db_session, _status("wamid.does-not-exist", "delivered"),
    )
    assert n == 0


# ---------------------------------------------------------------------------
# apply_status_updates — batch wrapper
# ---------------------------------------------------------------------------


def test_apply_status_updates_processes_each_receipt(db_session):
    _seed_log_row(db_session, meta_message_id="wamid.batch.1")
    _seed_log_row(db_session, meta_message_id="wamid.batch.2")
    total = apply_status_updates(db_session, [
        _status("wamid.batch.1", "sent", ts=1715817600),
        _status("wamid.batch.2", "delivered", ts=1715817700),
    ])
    assert total == 2
    rows = {
        r.meta_message_id: r for r in
        db_session.query(WhatsAppDeliveryLog).all()
    }
    assert rows["wamid.batch.1"].delivery_status == "sent"
    assert rows["wamid.batch.2"].delivery_status == "delivered"


def test_apply_status_updates_one_unmatched_does_not_drop_rest(db_session):
    _seed_log_row(db_session, meta_message_id="wamid.batch.match")
    total = apply_status_updates(db_session, [
        _status("wamid.does-not-exist", "sent"),
        _status("wamid.batch.match", "delivered"),
    ])
    # Only the matched receipt updated a row; the unmatched one returned 0.
    assert total == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.batch.match",
    ).one()
    assert row.delivery_status == "delivered"


def test_apply_status_updates_drives_off_parse_status_updates(db_session):
    """End-to-end-ish: payload -> parse_status_updates -> apply_status_updates.

    Confirms the consumer side wires together correctly without needing the
    Munshi FastAPI app — exercising the same data path the production
    webhook handler will use.
    """
    _seed_log_row(db_session, meta_message_id="wamid.out.001")
    _seed_log_row(db_session, meta_message_id="wamid.out.002")
    payload = json.loads(
        (_FIXTURE_DIR / "webhook_payload_text.json").read_text(encoding="utf-8")
    )
    statuses = parse_status_updates(payload)
    total = apply_status_updates(db_session, statuses)
    assert total == 2
    delivered = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.out.001",
    ).one()
    failed = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.out.002",
    ).one()
    assert delivered.delivery_status == "delivered"
    assert failed.delivery_status == "failed"
    # The fixture's failed receipt carries an errors[].title.
    assert failed.failure_reason == "Re-engagement message"


# ---------------------------------------------------------------------------
# Sentry capture (best-effort)
# ---------------------------------------------------------------------------


def test_apply_status_update_failed_captures_sentry_message(db_session, monkeypatch):
    """If sentry_sdk is importable, ``failed`` receipts produce a capture_message call."""
    captured = {}

    fake_sdk = types.SimpleNamespace(
        capture_message=lambda msg, level=None: captured.update({"msg": msg, "level": level}),
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)

    _seed_log_row(db_session, meta_message_id="wamid.failed.sentry")
    apply_status_update(
        db_session,
        _status("wamid.failed.sentry", "failed", failure_reason="Too long"),
    )
    assert "msg" in captured
    assert "Too long" in captured["msg"]
    assert captured["level"] == "warning"


def test_apply_status_update_failed_swallows_sentry_import_error(db_session, monkeypatch):
    """No sentry_sdk installed — handler still updates DB without raising."""
    # Force ImportError for ``import sentry_sdk`` inside the handler. Setting
    # the entry to ``None`` makes Python raise ImportError on import.
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    _seed_log_row(db_session, meta_message_id="wamid.failed.no-sentry")
    n = apply_status_update(
        db_session,
        _status("wamid.failed.no-sentry", "failed", failure_reason="x"),
    )
    assert n == 1
    row = db_session.query(WhatsAppDeliveryLog).filter_by(
        meta_message_id="wamid.failed.no-sentry",
    ).one()
    assert row.delivery_status == "failed"
