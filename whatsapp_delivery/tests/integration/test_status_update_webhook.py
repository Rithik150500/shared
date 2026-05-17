"""Integration: delivery receipt webhook updates the delivery_log row.

Per plan §19.2.1. After Meta accepts a send, it asynchronously POSTs
delivery-status receipts (sent / delivered / read / failed) to our
webhook. The handler maps each receipt to the row keyed by ``wamid``
and stamps the new status + timestamp.

This skeleton:
  1. Seeds a delivery_log row with a wamid.
  2. Parses a status-update payload and applies it via
     ``apply_status_update``.
  3. Asserts the row's ``delivery_status`` flipped + the matching
     timestamp column was set.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from data_access.models import WhatsAppDeliveryLog

from whatsapp_delivery.webhook.parser import DeliveryStatus
from whatsapp_delivery.webhook.status_handler import apply_status_update


def _seed_log_row(db_session, *, meta_message_id: str) -> WhatsAppDeliveryLog:
    row = WhatsAppDeliveryLog(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        rq_job_id=f"job-{meta_message_id}",
        template_name="nowlez_new_order_v1",
        brand="nowlez",
        delivery_status="pending",
        meta_message_id=meta_message_id,
        enqueued_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_delivered_receipt_flips_status_and_stamps_timestamp(db_session):
    """A 'delivered' receipt flips the row from pending to delivered."""
    seeded = _seed_log_row(db_session, meta_message_id="wamid.delivered-int")

    status = DeliveryStatus(
        meta_message_id="wamid.delivered-int",
        recipient_id="+919999999999",
        status="delivered",
        # Parser yields a unix int timestamp; the DAO converts to datetime.
        timestamp=int(datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc).timestamp()),
        failure_reason=None,
    )
    rowcount = apply_status_update(db_session, status)
    assert rowcount == 1

    db_session.refresh(seeded)
    assert seeded.delivery_status == "delivered"
    assert seeded.delivered_at is not None


def test_failed_receipt_stamps_failure_reason(db_session):
    """A 'failed' receipt records the failure reason for ops triage."""
    seeded = _seed_log_row(db_session, meta_message_id="wamid.failed-int")

    status = DeliveryStatus(
        meta_message_id="wamid.failed-int",
        recipient_id="+919999999999",
        status="failed",
        timestamp=int(datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc).timestamp()),
        failure_reason="Template paused by Meta",
    )
    rowcount = apply_status_update(db_session, status)
    assert rowcount == 1

    db_session.refresh(seeded)
    assert seeded.delivery_status == "failed"
    assert seeded.failure_reason == "Template paused by Meta"


def test_receipt_with_no_matching_row_is_noop(db_session):
    """A receipt whose wamid has no row yet (race against worker wamid-bind)
    must not raise — return value is 0 rowcount."""
    status = DeliveryStatus(
        meta_message_id="wamid.never-seen",
        recipient_id="+919999999999",
        status="sent",
        timestamp=int(datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc).timestamp()),
        failure_reason=None,
    )
    rowcount = apply_status_update(db_session, status)
    assert rowcount == 0
