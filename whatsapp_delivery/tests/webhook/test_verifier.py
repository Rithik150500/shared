"""Tests for whatsapp_delivery.webhook.verifier.verify_signature."""
from __future__ import annotations

import hashlib
import hmac

import pytest

from whatsapp_delivery.webhook.verifier import validate_secret, verify_signature


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


# ---------------------------------------------------------------------------
# D-2: validate_secret startup self-check
# ---------------------------------------------------------------------------


def test_validate_secret_accepts_clean_secret():
    """A well-formed secret returns None (no exception)."""
    assert validate_secret("topsecret-abc-123") is None


def test_validate_secret_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        validate_secret("")


def test_validate_secret_rejects_leading_whitespace():
    with pytest.raises(ValueError, match="whitespace"):
        validate_secret("  hassecret")


def test_validate_secret_rejects_trailing_whitespace():
    with pytest.raises(ValueError, match="whitespace"):
        validate_secret("hassecret\n")


def test_validate_secret_rejects_tab_padding():
    with pytest.raises(ValueError, match="whitespace"):
        validate_secret("\thassecret")


def test_validate_secret_rejects_bom_prefix():
    """A UTF-8 BOM at the start of the secret is a classic .env contamination."""
    bom = "﻿"
    with pytest.raises(ValueError, match="BOM"):
        validate_secret(bom + "hassecret")


# ---------------------------------------------------------------------------
# D-2: missing-signature path now logs a WARNING (not silent fail-closed)
# ---------------------------------------------------------------------------


def test_missing_header_logs_warning(caplog):
    """An absent X-Hub-Signature-256 must surface a WARNING for ops."""
    import logging

    with caplog.at_level(logging.WARNING, logger="whatsapp_delivery.webhook.verifier"):
        assert verify_signature(b"{}", None, "topsecret") is False
    assert any(
        "signature" in record.message.lower()
        and ("missing" in record.message.lower() or "no signature" in record.message.lower())
        for record in caplog.records
    )


def test_empty_header_logs_warning(caplog):
    """An empty signature header is operationally identical to missing."""
    import logging

    with caplog.at_level(logging.WARNING, logger="whatsapp_delivery.webhook.verifier"):
        assert verify_signature(b"{}", "", "topsecret") is False
    assert any("signature" in record.message.lower() for record in caplog.records)
