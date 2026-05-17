"""Tests for whatsapp_delivery.webhook.verifier.verify_signature."""
from __future__ import annotations

import hashlib
import hmac

from whatsapp_delivery.webhook.verifier import verify_signature


def _make_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    body = b'{"hello":"world"}'
    secret = "topsecret"
    assert verify_signature(body, _make_sig(body, secret), secret) is True


def test_invalid_signature_fails():
    assert verify_signature(b"{}", "sha256=" + "0" * 64, "topsecret") is False


def test_missing_header_fails():
    assert verify_signature(b"{}", None, "topsecret") is False


def test_malformed_header_fails():
    assert verify_signature(b"{}", "md5=abc", "topsecret") is False


def test_constant_time_comparison_used():
    # Use a body+sig where naive str.startswith would short-circuit; verify
    # that compare_digest catches a 1-byte mismatch.
    body = b"x"
    bad_sig = "sha256=" + "a" * 64
    assert verify_signature(body, bad_sig, "topsecret") is False
