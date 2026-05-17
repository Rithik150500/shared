"""Tests for the Razorpay webhook helpers (Task 4.6)."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from case_billing.razorpay_client.webhooks import (
    WebhookEvent,
    parse_webhook_event,
    verify_webhook_signature,
)


SECRET = "rzp_webhook_secret"


def _sign(raw_body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def test_verify_webhook_signature_accepts_valid() -> None:
    body = b'{"event": "payment.captured"}'
    assert verify_webhook_signature(body, _sign(body), SECRET) is True


def test_verify_webhook_signature_rejects_tampered_body() -> None:
    body = b'{"event": "payment.captured"}'
    sig = _sign(body)
    tampered = b'{"event": "payment.failed"}'
    assert verify_webhook_signature(tampered, sig, SECRET) is False


def test_verify_webhook_signature_rejects_wrong_secret() -> None:
    body = b'{"event": "payment.captured"}'
    sig = _sign(body, secret="someone_elses_secret")
    assert verify_webhook_signature(body, sig, SECRET) is False


def test_verify_webhook_signature_rejects_missing_signature() -> None:
    body = b'{"event": "payment.captured"}'
    assert verify_webhook_signature(body, "", SECRET) is False
    assert verify_webhook_signature(body, None, SECRET) is False  # type: ignore[arg-type]


def test_verify_webhook_signature_is_constant_time() -> None:
    """Smoke test: the helper still returns False for a hex string of the wrong length.

    The contract is that we never raise on malformed input — we just return False —
    so retry handlers don't need a defensive try/except.
    """
    body = b"x"
    assert verify_webhook_signature(body, "deadbeef", SECRET) is False
    assert verify_webhook_signature(body, "not-hex", SECRET) is False


def test_parse_webhook_event_extracts_canonical_fields() -> None:
    payload = {
        "entity": "event",
        "account_id": "acc_X",
        "event": "subscription.charged",
        "contains": ["subscription", "payment"],
        "payload": {
            "subscription": {"entity": {"id": "sub_1"}},
            "payment": {"entity": {"id": "pay_1"}},
        },
        "created_at": 1_700_000_000,
        "id": "evt_abc",
    }

    event = parse_webhook_event(payload)
    assert isinstance(event, WebhookEvent)
    assert event.id == "evt_abc"
    assert event.event == "subscription.charged"
    assert event.entity == "event"
    assert event.payload == payload["payload"]
    assert event.created_at == 1_700_000_000


def test_parse_webhook_event_handles_missing_optional_fields() -> None:
    payload = {"event": "payment.captured", "payload": {}}
    event = parse_webhook_event(payload)
    assert event.event == "payment.captured"
    assert event.id == ""
    assert event.entity == ""
    assert event.payload == {}
    assert event.created_at is None
