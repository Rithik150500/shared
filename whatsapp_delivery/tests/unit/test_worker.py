"""Tests for ``whatsapp_delivery.dispatch.worker``.

We test each job function directly (not via RQ) — RQ's own bookkeeping is
covered by the queue tests; here we exercise the send-side behaviour with
respx mocking the Meta Graph API.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from whatsapp_delivery.dispatch import worker as w
from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaTransientError,
)


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    """Every worker invocation builds a fresh WhatsAppConfig from env."""
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_NOWLEZ_DISABLED", raising=False)


# ---------------------------------------------------------------------------
# send_text
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_text_success():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.text"}]})
    )
    wamid = w._do_send_text(
        to="+919999999999", body="hi", brand="munshi", user_id=None,
    )
    assert wamid == "wamid.text"


@respx.mock
def test_do_send_text_5xx_propagates_transient_for_retry():
    """RQ's Retry policy needs the worker to re-raise transient errors."""
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(503, text="boom")
    )
    with pytest.raises(MetaTransientError):
        w._do_send_text(
            to="+919999999999", body="hi", brand="munshi", user_id=None,
        )


@respx.mock
def test_do_send_text_24h_window_dead_letters_and_raises(caplog):
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        w._do_send_text(
            to="+919999999999", body="hi", brand="nowlez", user_id="u-1",
        )
    # The dead-letter helper writes a logger.error line for ops.
    assert any("dead-letter" in r.message for r in caplog.records)


def test_do_send_text_nowlez_kill_switch_short_circuits(monkeypatch):
    monkeypatch.setenv("WHATSAPP_NOWLEZ_DISABLED", "1")
    out = w._do_send_text(
        to="+919999999999", body="hi", brand="nowlez", user_id=None,
    )
    assert out == ""


def test_do_send_text_munshi_kill_switch_does_not_apply(monkeypatch):
    """The nowlez kill switch must not affect munshi sends."""
    monkeypatch.setenv("WHATSAPP_NOWLEZ_DISABLED", "1")

    with respx.mock() as rmock:
        rmock.post("https://graph.facebook.com/v20.0/111/messages").mock(
            return_value=Response(
                200, json={"messages": [{"id": "wamid.munshi"}]}
            )
        )
        assert (
            w._do_send_text(
                to="+919999999999", body="hi", brand="munshi", user_id=None,
            )
            == "wamid.munshi"
        )


# ---------------------------------------------------------------------------
# send_template (registry-backed)
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_template_success_simple_template():
    """Welcome template: body var ``user_name``, URL button → /settings."""
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            200, json={"messages": [{"id": "wamid.tmpl"}]}
        )
    )
    wamid = w._do_send_template(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v2",
        language="en_US",
        variables={"user_name": "Asha"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=None,
    )
    assert wamid == "wamid.tmpl"


@respx.mock
def test_do_send_template_document_header_uploads_media_first():
    """new_order template has a document header so media must upload first."""
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-123"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.pdf"}]})
    )

    wamid = w._do_send_template(
        to="+919999999999",
        template_name="nowlez_new_order_v1",
        language="en_US",
        variables={
            "case_title": "Mehta v. State",
            "order_date": "01 Jan 2026",
            "order_descriptive_name": "Final Order",
            "case_id": "case-uuid",
        },
        brand="nowlez",
        media_bytes=b"%PDF-1.4\n...",
        media_filename="order.pdf",
        media_mime="application/pdf",
        related_case_id="case-uuid",
        related_order_id=None,
        user_id="u-1",
    )
    assert wamid == "wamid.pdf"


def test_do_send_template_nowlez_kill_switch_short_circuits(monkeypatch):
    monkeypatch.setenv("WHATSAPP_NOWLEZ_DISABLED", "1")
    out = w._do_send_template(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v2",
        language="en_US",
        variables={"user_name": "Asha"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=None,
    )
    assert out == ""


# ---------------------------------------------------------------------------
# send_template_with_components (positional vars)
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_template_with_components_success():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            200, json={"messages": [{"id": "wamid.legacy"}]}
        )
    )
    wamid = w._do_send_template_with_components(
        to="+919999999999",
        template_name="new_order_v1",  # Munshi short name
        language="en",
        body_variables=["Mehta v. State", "01 Jan 2026", "ORD-1"],
        brand="munshi",
        header_media_id=None,
        button_url_variables=None,
        user_id=None,
    )
    assert wamid == "wamid.legacy"


# ---------------------------------------------------------------------------
# send_document
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_document_uploads_then_sends(tmp_path):
    """D-5: ``document_url=file:///path`` is read from disk then uploaded."""
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4\n...")

    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-xyz"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.doc"}]})
    )
    wamid = w._do_send_document(
        to="+919999999999",
        document_url=f"file:///{pdf.as_posix().lstrip('/')}",
        caption="Order PDF",
        filename="order.pdf",
        brand="munshi",
    )
    assert wamid == "wamid.doc"


# ---------------------------------------------------------------------------
# Additional error-path coverage (Task 19.1.1 coverage uplift)
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_template_5xx_propagates_transient_for_retry():
    """A 5xx during the template send must re-raise so RQ retries."""
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(503, text="upstream boom")
    )
    with pytest.raises(MetaTransientError):
        w._do_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v2",
            language="en_US",
            variables={"user_name": "Asha"},
            brand="nowlez",
            media_bytes=None,
            media_filename=None,
            media_mime="application/pdf",
            related_case_id=None,
            related_order_id=None,
            user_id=None,
        )


@respx.mock
def test_do_send_template_24h_window_dead_letters_and_raises(caplog):
    """Templates SHOULD bypass 24h; if we see 131047, dead-letter + re-raise."""
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        w._do_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v2",
            language="en_US",
            variables={"user_name": "Asha"},
            brand="nowlez",
            media_bytes=None,
            media_filename=None,
            media_mime="application/pdf",
            related_case_id=None,
            related_order_id=None,
            user_id="u-1",
        )
    assert any("dead-letter" in r.message for r in caplog.records)


@respx.mock
def test_do_send_template_with_components_5xx_propagates_transient():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(503, text="boom")
    )
    with pytest.raises(MetaTransientError):
        w._do_send_template_with_components(
            to="+919999999999",
            template_name="any_template_v1",
            language="en",
            body_variables=["X"],
            brand="munshi",
            header_media_id=None,
            button_url_variables=None,
            user_id=None,
        )


@respx.mock
def test_do_send_template_with_components_24h_dead_letters_and_raises(caplog):
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        w._do_send_template_with_components(
            to="+919999999999",
            template_name="any_template_v1",
            language="en",
            body_variables=["X"],
            brand="nowlez",
            header_media_id=None,
            button_url_variables=None,
            user_id="u-1",
        )
    assert any("dead-letter" in r.message for r in caplog.records)


def test_do_send_template_with_components_nowlez_kill_switch_short_circuits(monkeypatch):
    monkeypatch.setenv("WHATSAPP_NOWLEZ_DISABLED", "1")
    out = w._do_send_template_with_components(
        to="+919999999999",
        template_name="any_template_v1",
        language="en",
        body_variables=["X"],
        brand="nowlez",
        header_media_id=None,
        button_url_variables=None,
        user_id=None,
    )
    assert out == ""


def test_do_send_document_nowlez_kill_switch_short_circuits(monkeypatch):
    """Kill switch runs before URL resolution — a bogus path is fine."""
    monkeypatch.setenv("WHATSAPP_NOWLEZ_DISABLED", "1")
    out = w._do_send_document(
        to="+919999999999",
        document_url="file:///nonexistent/x.pdf",
        caption="cap",
        filename="x.pdf",
        brand="nowlez",
    )
    assert out == ""


@respx.mock
def test_do_send_document_5xx_propagates_transient(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n...")

    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(503, text="boom")
    )
    with pytest.raises(MetaTransientError):
        w._do_send_document(
            to="+919999999999",
            document_url=f"file:///{pdf.as_posix().lstrip('/')}",
            caption="cap",
            filename="x.pdf",
            brand="munshi",
        )


@respx.mock
def test_do_send_document_24h_dead_letters_and_raises(caplog, tmp_path):
    """A 24h error on document send dead-letters (and re-raises)."""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n...")

    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-doc"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        w._do_send_document(
            to="+919876543210",
            document_url=f"file:///{pdf.as_posix().lstrip('/')}",
            caption="cap",
            filename="x.pdf",
            brand="nowlez",
        )
    assert any("dead-letter" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# D-3: phone redaction in dead-letter logs / ctx
# ---------------------------------------------------------------------------


def test_redact_phone_returns_last_4_digits():
    assert w._redact_phone("+919876543210") == "***3210"


def test_redact_phone_short_input_does_not_crash():
    """A pathological short input still returns *something* — never a full leak."""
    out = w._redact_phone("12")
    assert "12" not in out or out.startswith("***")


def test_redact_phone_handles_none():
    """None inputs are safe (returned as a constant placeholder)."""
    assert w._redact_phone(None) == "***"


def test_redact_phone_strips_non_digits():
    """A phone with formatting still ends with the underlying last 4 digits."""
    assert w._redact_phone("+91-98765-43210") == "***3210"


@respx.mock
def test_dead_letter_log_redacts_phone(caplog):
    """The dead-letter helper must never log the full phone."""
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        w._do_send_text(
            to="+919876543210", body="hi", brand="nowlez", user_id="u-1",
        )
    # No record should contain the full phone, but the redacted form
    # SHOULD appear so operators can match against support tickets.
    for rec in caplog.records:
        # The message text after format substitution
        assert "+919876543210" not in rec.getMessage()
        assert "919876543210" not in rec.getMessage()
    assert any("***3210" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# D-3: _sanitize_meta_response — bearer/JWT scrubbing in meta_client
# ---------------------------------------------------------------------------


def test_sanitize_meta_response_strips_bearer_token():
    from whatsapp_delivery.meta_client import _sanitize_meta_response

    out = _sanitize_meta_response("error: Bearer abc123tokensecret was invalid")
    assert "abc123tokensecret" not in out
    assert "Bearer <redacted>" in out


def test_sanitize_meta_response_strips_authorization_header():
    """An echoed Authorization: ... header line is scrubbed.

    Real-world Meta error bodies sometimes echo request headers in the
    debug envelope. Both ``Authorization: <value>`` and bare ``Bearer
    <token>`` forms must be redacted before raising.
    """
    from whatsapp_delivery.meta_client import _sanitize_meta_response

    secret = "supersecrettoken123XYZ"
    out = _sanitize_meta_response(f"debug headers Authorization: {secret} end")
    assert secret not in out
    assert "<redacted>" in out


def test_sanitize_meta_response_strips_jwt():
    from whatsapp_delivery.meta_client import _sanitize_meta_response

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_x"
    out = _sanitize_meta_response(f"echoed back: {jwt} bye")
    assert jwt not in out
    assert "<redacted-jwt>" in out


def test_sanitize_meta_response_truncates_to_200_chars():
    from whatsapp_delivery.meta_client import _sanitize_meta_response

    long = "x" * 1000
    out = _sanitize_meta_response(long)
    assert len(out) == 200


def test_sanitize_meta_response_handles_empty():
    from whatsapp_delivery.meta_client import _sanitize_meta_response

    assert _sanitize_meta_response("") == ""
