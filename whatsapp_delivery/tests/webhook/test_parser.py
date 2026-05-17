"""Tests for whatsapp_delivery.webhook.parser.

Uses a representative Meta webhook payload fixture (one text inbound + two
delivery statuses, one of them a failure with an errors[] payload).
"""
from __future__ import annotations

import json
from pathlib import Path

from whatsapp_delivery.webhook.parser import (
    DeliveryStatus,
    IncomingButton,
    IncomingMedia,
    IncomingMessage,
    parse_incoming,
    parse_status_updates,
)


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


# --- parse_incoming ---

def test_parse_incoming_text_message_from_fixture():
    payload = _load("webhook_payload_text.json")
    msgs = parse_incoming(payload)
    assert len(msgs) == 1
    m = msgs[0]
    assert isinstance(m, IncomingMessage)
    assert m.meta_message_id == "wamid.text.001"
    assert m.from_phone == "+919876543210"  # leading '+' prepended
    assert m.timestamp == 1715817600
    assert m.type == "text"
    assert m.text == "hello world"
    assert m.media is None
    assert m.button is None


def test_parse_incoming_skips_status_only_payloads():
    payload = {
        "entry": [
            {"changes": [
                {"value": {"statuses": [
                    {"id": "wamid.x", "status": "delivered",
                     "timestamp": "1715817700", "recipient_id": "919876543210"}
                ]}}
            ]}
        ]
    }
    assert parse_incoming(payload) == []


def test_parse_incoming_image_media():
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {
                "from": "919876543210",
                "id": "wamid.img.001",
                "timestamp": "1715817600",
                "type": "image",
                "image": {"id": "media-img-1", "mime_type": "image/jpeg", "sha256": "abc"},
            }
        ]}}]}]
    }
    msgs = parse_incoming(payload)
    assert len(msgs) == 1
    assert isinstance(msgs[0].media, IncomingMedia)
    assert msgs[0].media.media_id == "media-img-1"
    assert msgs[0].media.mime_type == "image/jpeg"
    assert msgs[0].media.sha256 == "abc"


def test_parse_incoming_interactive_button_reply():
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {
                "from": "919876543210",
                "id": "wamid.btn.001",
                "timestamp": "1715817600",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "yes", "title": "Yes"},
                },
            }
        ]}}]}]
    }
    msgs = parse_incoming(payload)
    assert isinstance(msgs[0].button, IncomingButton)
    assert msgs[0].button.button_id == "yes"
    assert msgs[0].button.title == "Yes"


def test_parse_incoming_template_button_reply():
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {
                "from": "919876543210",
                "id": "wamid.tbtn.001",
                "timestamp": "1715817600",
                "type": "button",
                "button": {"payload": "STOP", "text": "Stop notifications"},
            }
        ]}}]}]
    }
    msgs = parse_incoming(payload)
    assert msgs[0].button.button_id == "STOP"
    assert msgs[0].button.title == "Stop notifications"


def test_parse_incoming_preserves_already_prefixed_phone():
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {
                "from": "+919876543210",
                "id": "wamid.plus.001",
                "timestamp": "1715817600",
                "type": "text",
                "text": {"body": "hi"},
            }
        ]}}]}]
    }
    assert parse_incoming(payload)[0].from_phone == "+919876543210"


# --- parse_status_updates ---

def test_parse_status_updates_from_fixture():
    payload = _load("webhook_payload_text.json")
    statuses = parse_status_updates(payload)
    assert len(statuses) == 2

    delivered, failed = statuses
    assert isinstance(delivered, DeliveryStatus)
    assert delivered.meta_message_id == "wamid.out.001"
    assert delivered.recipient_id == "919876543210"
    assert delivered.status == "delivered"
    assert delivered.timestamp == 1715817700
    assert delivered.failure_reason is None

    assert failed.meta_message_id == "wamid.out.002"
    assert failed.status == "failed"
    assert failed.timestamp == 1715817800
    # 'title' is preferred over 'message' when both present.
    assert failed.failure_reason == "Re-engagement message"


def test_parse_status_updates_skips_message_only_payloads():
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "919876543210", "id": "wamid.x", "timestamp": "1",
             "type": "text", "text": {"body": "hi"}}
        ]}}]}]
    }
    assert parse_status_updates(payload) == []


def test_parse_status_updates_failure_without_title_uses_message():
    payload = {
        "entry": [{"changes": [{"value": {"statuses": [
            {
                "id": "wamid.f.1",
                "status": "failed",
                "timestamp": "1715817800",
                "recipient_id": "919876543210",
                "errors": [{"code": 131000, "message": "Generic failure"}],
            }
        ]}}]}]
    }
    [s] = parse_status_updates(payload)
    assert s.failure_reason == "Generic failure"


def test_parse_status_updates_empty_payload():
    assert parse_status_updates({}) == []
    assert parse_status_updates({"entry": []}) == []
