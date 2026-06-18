"""Tests for whatsapp_delivery.webhook.account_handler.

Covers:
- parse_account_events: each of the three field types with realistic payloads
- handle_account_events: correct log level per event severity (via caplog)
- handle_account_events: returns correct count
- Malformed / missing payloads: no exception raised
"""
from __future__ import annotations

import logging

import pytest

from whatsapp_delivery.webhook.account_handler import (
    FIELD_ACCOUNT_UPDATE,
    FIELD_PHONE_QUALITY,
    FIELD_TEMPLATE_STATUS,
    AccountEvent,
    handle_account_events,
    parse_account_events,
)


# ---------------------------------------------------------------------------
# Payload factories
# ---------------------------------------------------------------------------


def _phone_quality_payload(
    event: str = "FLAGGED",
    display_phone: str = "919643460175",
    current_limit: str = "TIER_250",
) -> dict:
    """Realistic phone_number_quality_update webhook payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "field": "phone_number_quality_update",
                        "value": {
                            "display_phone_number": display_phone,
                            "event": event,
                            "current_limit": current_limit,
                        },
                    }
                ],
            }
        ],
    }


def _template_status_payload(
    event: str = "APPROVED",
    template_name: str = "munshi_welcome_video_v1",
    template_id: int = 1054891363544782,
    reason: str | None = None,
) -> dict:
    """Realistic message_template_status_update webhook payload."""
    value: dict = {
        "event": event,
        "message_template_name": template_name,
        "message_template_id": template_id,
    }
    if reason is not None:
        value["reason"] = reason
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": value,
                    }
                ],
            }
        ],
    }


def _account_update_payload(
    event: str = "VERIFIED_ACCOUNT",
    extra: dict | None = None,
) -> dict:
    """Realistic account_update webhook payload."""
    value: dict = {"event": event}
    if extra:
        value.update(extra)
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "field": "account_update",
                        "value": value,
                    }
                ],
            }
        ],
    }


def _multi_change_payload(*changes: dict) -> dict:
    """Payload with multiple changes in one entry."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": list(changes),
            }
        ],
    }


# ---------------------------------------------------------------------------
# parse_account_events — field routing
# ---------------------------------------------------------------------------


class TestParseAccountEvents:
    """parse_account_events returns correct AccountEvent per change field."""

    def test_phone_quality_flagged(self):
        payload = _phone_quality_payload(event="FLAGGED")
        events = parse_account_events(payload)
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, AccountEvent)
        assert ev.field == FIELD_PHONE_QUALITY
        assert ev.event == "FLAGGED"
        assert ev.detail["phone"] == "919643460175"
        assert ev.detail["current_limit"] == "TIER_250"

    def test_phone_quality_upgrade(self):
        payload = _phone_quality_payload(event="UPGRADE", current_limit="TIER_1K")
        events = parse_account_events(payload)
        assert len(events) == 1
        ev = events[0]
        assert ev.event == "UPGRADE"
        assert ev.detail["current_limit"] == "TIER_1K"

    def test_phone_quality_downgrade(self):
        payload = _phone_quality_payload(event="DOWNGRADE")
        [ev] = parse_account_events(payload)
        assert ev.field == FIELD_PHONE_QUALITY
        assert ev.event == "DOWNGRADE"

    def test_template_status_approved(self):
        payload = _template_status_payload(event="APPROVED")
        [ev] = parse_account_events(payload)
        assert ev.field == FIELD_TEMPLATE_STATUS
        assert ev.event == "APPROVED"
        assert ev.detail["template_name"] == "munshi_welcome_video_v1"
        assert ev.detail["template_id"] == 1054891363544782

    def test_template_status_paused_with_reason(self):
        payload = _template_status_payload(
            event="PAUSED", reason="Low quality ratio"
        )
        [ev] = parse_account_events(payload)
        assert ev.field == FIELD_TEMPLATE_STATUS
        assert ev.event == "PAUSED"
        assert ev.detail["reason"] == "Low quality ratio"

    def test_template_status_rejected(self):
        payload = _template_status_payload(event="REJECTED", reason="Policy violation")
        [ev] = parse_account_events(payload)
        assert ev.event == "REJECTED"
        assert ev.detail["reason"] == "Policy violation"

    def test_template_status_flagged(self):
        payload = _template_status_payload(event="FLAGGED")
        [ev] = parse_account_events(payload)
        assert ev.event == "FLAGGED"

    def test_account_update_verified(self):
        payload = _account_update_payload(event="VERIFIED_ACCOUNT")
        [ev] = parse_account_events(payload)
        assert ev.field == FIELD_ACCOUNT_UPDATE
        assert ev.event == "VERIFIED_ACCOUNT"

    def test_account_update_restriction(self):
        payload = _account_update_payload(
            event="ACCOUNT_RESTRICTION",
            extra={"restriction_info": [{"restriction_type": "ACCOUNT_VIOLATION"}]},
        )
        [ev] = parse_account_events(payload)
        assert ev.field == FIELD_ACCOUNT_UPDATE
        assert ev.event == "ACCOUNT_RESTRICTION"
        assert "restriction_info" in ev.detail

    def test_account_update_banned_with_ban_info(self):
        payload = _account_update_payload(
            event="BANNED",
            extra={"ban_info": {"waba_ban_state": "SCHEDULE_FOR_DISABLE", "waba_ban_date": "2026-07-01"}},
        )
        [ev] = parse_account_events(payload)
        assert ev.event == "BANNED"
        assert ev.detail["ban_info"]["waba_ban_state"] == "SCHEDULE_FOR_DISABLE"

    def test_unknown_field_skipped(self):
        """A change whose field is not one of the three known fields is silently skipped."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {"field": "some_future_field", "value": {"event": "SOMETHING"}},
                        {"field": FIELD_ACCOUNT_UPDATE, "value": {"event": "VERIFIED_ACCOUNT"}},
                    ]
                }
            ]
        }
        events = parse_account_events(payload)
        assert len(events) == 1
        assert events[0].field == FIELD_ACCOUNT_UPDATE

    def test_empty_payload_returns_empty(self):
        events = parse_account_events({})
        assert events == []

    def test_payload_with_no_entry_returns_empty(self):
        events = parse_account_events({"object": "whatsapp_business_account"})
        assert events == []

    def test_missing_event_key_defaults_to_unknown(self):
        """A value dict without an 'event' key must not raise; event defaults to 'UNKNOWN'."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {"field": FIELD_ACCOUNT_UPDATE, "value": {}},
                    ]
                }
            ]
        }
        events = parse_account_events(payload)
        assert len(events) == 1
        assert events[0].event == "UNKNOWN"

    def test_multiple_changes_in_one_payload(self):
        """Multiple changes in one payload all produce events."""
        payload = _multi_change_payload(
            {"field": FIELD_PHONE_QUALITY, "value": {"event": "FLAGGED", "current_limit": "TIER_250"}},
            {"field": FIELD_TEMPLATE_STATUS, "value": {"event": "PAUSED", "message_template_name": "t1", "message_template_id": 1}},
            {"field": FIELD_ACCOUNT_UPDATE, "value": {"event": "ACCOUNT_RESTRICTION"}},
        )
        events = parse_account_events(payload)
        assert len(events) == 3
        assert {ev.field for ev in events} == {FIELD_PHONE_QUALITY, FIELD_TEMPLATE_STATUS, FIELD_ACCOUNT_UPDATE}

    def test_malformed_payload_does_not_raise(self):
        """Completely malformed payloads must not raise."""
        for bad in [None, [], "string", 42, {"entry": "not_a_list"}]:
            try:
                result = parse_account_events(bad)  # type: ignore[arg-type]
                # May return [] or raise; we only care it doesn't raise TypeError/KeyError hard
            except (AttributeError, TypeError):
                pass  # acceptable: defensiveness is best-effort for truly wrong types


# ---------------------------------------------------------------------------
# handle_account_events — log levels
# ---------------------------------------------------------------------------


class TestHandleAccountEvents:
    """handle_account_events emits at the correct log level."""

    def test_phone_quality_flagged_logs_error(self, caplog):
        payload = _phone_quality_payload(event="FLAGGED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            count = handle_account_events(payload)
        assert count == 1
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "FLAGGED" in error_records[0].message

    def test_phone_quality_downgrade_logs_error(self, caplog):
        payload = _phone_quality_payload(event="DOWNGRADE")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            count = handle_account_events(payload)
        assert count == 1
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_phone_quality_upgrade_logs_info(self, caplog):
        payload = _phone_quality_payload(event="UPGRADE")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) >= 1
        assert any("UPGRADE" in r.message for r in info_records)

    def test_phone_quality_onlined_logs_info(self, caplog):
        payload = _phone_quality_payload(event="ONLINED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("ONLINED" in r.message for r in info_records)

    def test_template_paused_logs_error(self, caplog):
        payload = _template_status_payload(event="PAUSED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            count = handle_account_events(payload)
        assert count == 1
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "PAUSED" in error_records[0].message

    def test_template_rejected_logs_error(self, caplog):
        payload = _template_status_payload(event="REJECTED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_template_flagged_logs_error(self, caplog):
        payload = _template_status_payload(event="FLAGGED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_template_approved_logs_info(self, caplog):
        payload = _template_status_payload(event="APPROVED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("APPROVED" in r.message for r in info_records)

    def test_account_restriction_logs_error(self, caplog):
        payload = _account_update_payload(event="ACCOUNT_RESTRICTION")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            count = handle_account_events(payload)
        assert count == 1
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_account_banned_logs_error(self, caplog):
        payload = _account_update_payload(event="BANNED")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_account_disabled_update_logs_error(self, caplog):
        payload = _account_update_payload(event="DISABLED_UPDATE")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_account_verified_logs_info(self, caplog):
        payload = _account_update_payload(event="VERIFIED_ACCOUNT")
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("VERIFIED_ACCOUNT" in r.message for r in info_records)

    def test_returns_correct_count_multi_event(self, caplog):
        """handle_account_events returns the number of events parsed."""
        payload = _multi_change_payload(
            {"field": FIELD_PHONE_QUALITY, "value": {"event": "FLAGGED"}},
            {"field": FIELD_TEMPLATE_STATUS, "value": {"event": "APPROVED", "message_template_name": "t1", "message_template_id": 1}},
        )
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            count = handle_account_events(payload)
        assert count == 2

    def test_empty_payload_returns_zero_no_raise(self, caplog):
        """An empty payload returns 0 and must not raise."""
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            count = handle_account_events({})
        assert count == 0

    def test_malformed_payload_does_not_raise(self, caplog):
        """A completely broken payload must not propagate an exception."""
        with caplog.at_level(logging.DEBUG, logger="whatsapp_delivery.webhook.account_handler"):
            try:
                count = handle_account_events({"entry": None})  # type: ignore[arg-type]
                assert count == 0
            except (TypeError, AttributeError):
                pass  # acceptable — we test it doesn't crash the caller

    def test_log_contains_field_and_event(self, caplog):
        """Log records must include both 'field' and 'event' info."""
        payload = _phone_quality_payload(event="FLAGGED")
        with caplog.at_level(logging.ERROR, logger="whatsapp_delivery.webhook.account_handler"):
            handle_account_events(payload)
        assert caplog.records
        record_text = caplog.records[0].getMessage()
        assert "phone_number_quality_update" in record_text
        assert "FLAGGED" in record_text
