"""Tests for Tasks 6a/6b: broadcast ledger routing via the status webhook.

Covers:
- parse_status_updates populates error_code from errors[].code (6a)
- apply_status_update calls broadcast_dao.apply_broadcast_status with error_code (6b)
- error_code 131026 also triggers broadcast_dao.suppress with the row's wa_digits (6b)

Uses an in-memory SQLite session with a pre-seeded broadcast ledger row
(claim_send + mark_sent for wamid.9), mirroring how test_status_handler.py
sets up its WhatsAppDeliveryLog fixtures. Meta-side I/O (the actual HTTP send)
was already mocked in the driver tests; here we only need to seed the DB and
drive the real handler.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import data_access.models  # noqa: F401 — register all models
from data_access.base import Base
from data_access.daos import broadcast_dao
from data_access.models.broadcast import WaBroadcastLog, WaSuppression

from whatsapp_delivery.webhook.parser import DeliveryStatus, parse_status_updates
from whatsapp_delivery.webhook.status_handler import apply_status_update


# ---------------------------------------------------------------------------
# SQLite in-memory fixture (same pattern as test_status_handler.py)
# ---------------------------------------------------------------------------

sqlite3.register_adapter(uuid.UUID, str)


@pytest.fixture()
def db_session():
    """Per-test in-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WAMID = "wamid.9"
_WA_DIGITS = "919876543210"
_CAMPAIGN = "test_broadcast_2026"


def _seed_broadcast_row(session) -> None:
    """Insert a broadcast ledger row at status=sent with meta_message_id=wamid.9."""
    broadcast_dao.claim_send(
        session,
        campaign=_CAMPAIGN,
        wa_digits=_WA_DIGITS,
        tier="T1",
        template_name="munshi_welcome_video_v1",
        language="en",
    )
    broadcast_dao.mark_sent(
        session,
        campaign=_CAMPAIGN,
        wa_digits=_WA_DIGITS,
        wamid=_WAMID,
    )
    session.flush()


def _make_failed_131026_payload() -> dict:
    """Build a realistic Meta webhook payload for a failed message with code 131026."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789012345",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "919999999999",
                                "phone_number_id": "111222333",
                            },
                            "statuses": [
                                {
                                    "id": _WAMID,
                                    "status": "failed",
                                    "timestamp": "1715817800",
                                    "recipient_id": _WA_DIGITS,
                                    "errors": [
                                        {
                                            "code": 131026,
                                            "title": "Message undeliverable",
                                            "message": (
                                                "Message undeliverable: recipient is not "
                                                "a WhatsApp user."
                                            ),
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _make_status(
    *,
    wamid: str = _WAMID,
    status: str = "failed",
    error_code: int | None = 131026,
    failure_reason: str | None = "Message undeliverable",
    timestamp: int = 1715817800,
) -> DeliveryStatus:
    return DeliveryStatus(
        meta_message_id=wamid,
        recipient_id=_WA_DIGITS,
        status=status,
        timestamp=timestamp,
        failure_reason=failure_reason,
        error_code=error_code,
    )


# ---------------------------------------------------------------------------
# 6a — parse_status_updates populates error_code
# ---------------------------------------------------------------------------


def test_parse_status_updates_populates_error_code():
    """parse_status_updates must extract error_code from errors[].code (int)."""
    payload = _make_failed_131026_payload()
    statuses = parse_status_updates(payload)
    assert len(statuses) == 1
    st = statuses[0]
    assert st.meta_message_id == _WAMID
    assert st.status == "failed"
    assert st.error_code == 131026
    assert st.failure_reason == "Message undeliverable"


def test_parse_status_updates_error_code_none_when_no_errors():
    """error_code must be None for receipts without an errors[] array."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.delivered",
                                    "status": "delivered",
                                    "timestamp": "1715817700",
                                    "recipient_id": _WA_DIGITS,
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    [st] = parse_status_updates(payload)
    assert st.error_code is None
    assert st.failure_reason is None


def test_parse_status_updates_error_code_is_int():
    """error_code must be an int (not a string) even if Meta sends it as a number."""
    payload = _make_failed_131026_payload()
    [st] = parse_status_updates(payload)
    assert isinstance(st.error_code, int)


# ---------------------------------------------------------------------------
# 6b — apply_status_update routes to broadcast ledger
# ---------------------------------------------------------------------------


def test_apply_status_update_calls_apply_broadcast_status(db_session):
    """apply_status_update must update the broadcast ledger row for the wamid."""
    _seed_broadcast_row(db_session)

    st = _make_status()
    apply_status_update(db_session, st)

    # Verify the broadcast row was updated to 'failed' with error_code=131026
    row = db_session.execute(
        select(WaBroadcastLog).where(WaBroadcastLog.meta_message_id == _WAMID)
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "failed"
    assert row.error_code == 131026
    assert row.failure_reason == "Message undeliverable"
    assert row.failed_at is not None


def test_apply_status_update_131026_suppresses_wa_digits(db_session):
    """A 131026 failure must add the recipient's wa_digits to the suppression list."""
    _seed_broadcast_row(db_session)

    st = _make_status(error_code=131026)
    apply_status_update(db_session, st)

    suppressed = db_session.execute(
        select(WaSuppression).where(WaSuppression.wa_digits == _WA_DIGITS)
    ).scalar_one_or_none()
    assert suppressed is not None
    assert suppressed.reason == "undeliverable"
    assert suppressed.source == "status_131026"


def test_apply_status_update_non_131026_does_not_suppress(db_session):
    """A failed receipt with a different error code must NOT suppress the number."""
    _seed_broadcast_row(db_session)

    st = _make_status(error_code=131047, failure_reason="Re-engagement message")
    apply_status_update(db_session, st)

    suppressed = db_session.execute(
        select(WaSuppression).where(WaSuppression.wa_digits == _WA_DIGITS)
    ).scalar_one_or_none()
    assert suppressed is None


def test_apply_status_update_no_broadcast_row_still_succeeds(db_session):
    """If the wamid has no broadcast ledger row (regular user message), no exception."""
    st = _make_status(wamid="wamid.user.ordinary")
    # No broadcast row seeded — apply_broadcast_status returns 0, no suppress triggered
    apply_status_update(db_session, st)  # must not raise


def test_apply_status_update_delivered_updates_broadcast_row(db_session):
    """A 'delivered' receipt stamps the broadcast row's delivered_at and status."""
    _seed_broadcast_row(db_session)

    st = DeliveryStatus(
        meta_message_id=_WAMID,
        recipient_id=_WA_DIGITS,
        status="delivered",
        timestamp=1715817700,
        failure_reason=None,
        error_code=None,
    )
    apply_status_update(db_session, st)

    row = db_session.execute(
        select(WaBroadcastLog).where(WaBroadcastLog.meta_message_id == _WAMID)
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert row.error_code is None


# ---------------------------------------------------------------------------
# End-to-end: payload -> parse -> apply (6a + 6b together)
# ---------------------------------------------------------------------------


def test_e2e_parse_then_apply_131026(db_session):
    """Full round-trip: Meta payload -> parse_status_updates -> apply_status_update."""
    _seed_broadcast_row(db_session)

    payload = _make_failed_131026_payload()
    statuses = parse_status_updates(payload)
    assert len(statuses) == 1
    assert statuses[0].error_code == 131026

    apply_status_update(db_session, statuses[0])

    # Ledger updated
    row = db_session.execute(
        select(WaBroadcastLog).where(WaBroadcastLog.meta_message_id == _WAMID)
    ).scalar_one_or_none()
    assert row.status == "failed"
    assert row.error_code == 131026

    # Suppressed
    sup = db_session.execute(
        select(WaSuppression).where(WaSuppression.wa_digits == _WA_DIGITS)
    ).scalar_one_or_none()
    assert sup is not None
    assert sup.reason == "undeliverable"
    assert sup.source == "status_131026"
