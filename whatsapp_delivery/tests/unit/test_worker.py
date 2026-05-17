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
        template_name="nowlez_signup_welcome_v1",
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
        template_name="nowlez_signup_welcome_v1",
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
def test_do_send_document_uploads_then_sends():
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-xyz"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.doc"}]})
    )
    wamid = w._do_send_document(
        to="+919999999999",
        document_bytes=b"%PDF-1.4\n...",
        caption="Order PDF",
        filename="order.pdf",
        brand="munshi",
    )
    assert wamid == "wamid.doc"
