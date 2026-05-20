"""Tests for whatsapp_delivery.template_client.TemplateClient.

Covers the new send_template_with_components method (spec §3.9): body-only,
body + document header, body + URL button, and the full new_order shape
(body + header + button).
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
)
from whatsapp_delivery.template_client import MetaTemplateClient, TemplateClient


_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


def _make_client() -> TemplateClient:
    return TemplateClient(phone_number_id="111", access_token="tok")


def _captured_body(route) -> dict:
    """Pull the JSON body out of the most recent intercepted respx call."""
    call = route.calls.last
    return json.loads(call.request.content.decode())


def test_alias_class_name_matches():
    """MetaTemplateClient is exported as a back-compat alias to TemplateClient."""
    assert MetaTemplateClient is TemplateClient


@respx.mock
def test_send_template_basic_body_only():
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.bare"}]})
    )
    wamid = _make_client().send_template(
        to="+919999999999",
        name="welcome_v1",
        language="en_US",
        variables=["Alice"],
    )
    assert wamid == "wamid.bare"
    body = _captured_body(route)
    template = body["template"]
    assert template["name"] == "welcome_v1"
    assert template["language"] == {"code": "en_US"}
    assert template["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Alice"}]}
    ]
    assert body["to"] == "919999999999"  # leading '+' stripped


@respx.mock
def test_send_template_5xx_raises_transient():
    respx.post(_MESSAGES_URL).mock(return_value=Response(503, text="boom"))
    with pytest.raises(MetaTransientError):
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=[],
        )


@respx.mock
def test_send_template_24h_window_raises():
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=[],
        )


@respx.mock
def test_send_template_other_4xx_raises_invalid():
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            400, json={"error": {"code": 132000, "message": "no such template"}}
        )
    )
    with pytest.raises(MetaInvalidMessage):
        _make_client().send_template(
            to="+919999999999", name="missing", language="en_US", variables=[],
        )


# ---------------------------------------------------------------------------
# D-1: template path mirrors the meta_client retry classification
# ---------------------------------------------------------------------------


@respx.mock
def test_send_template_429_raises_transient():
    """A 429 on the template endpoint must surface as MetaTransientError."""
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(429, text="too many requests")
    )
    with pytest.raises(MetaTransientError):
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=[],
        )


@respx.mock
def test_send_template_429_with_retry_after_surfaces_seconds():
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(429, text="slow", headers={"Retry-After": "5"})
    )
    with pytest.raises(MetaTransientError) as exc_info:
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=[],
        )
    assert exc_info.value.retry_after_seconds == 5


@respx.mock
def test_send_template_meta_error_code_130429_is_transient():
    """Application-level rate-limit code is retry-able on the template path."""
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            400, json={"error": {"code": 130429, "message": "rate limit"}}
        )
    )
    with pytest.raises(MetaTransientError):
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=[],
        )


# --- send_template_with_components (spec §3.9) ---

@respx.mock
def test_send_template_with_components_body_only():
    """Body-only path: no header, no buttons -- single body component."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.body"}]})
    )
    wamid = _make_client().send_template_with_components(
        to="+919999999999",
        name="welcome_v1",
        language="en_US",
        body_variables=["Alice", "Smith vs Bank"],
    )
    assert wamid == "wamid.body"
    body = _captured_body(route)
    components = body["template"]["components"]
    assert len(components) == 1
    assert components[0] == {
        "type": "body",
        "parameters": [
            {"type": "text", "text": "Alice"},
            {"type": "text", "text": "Smith vs Bank"},
        ],
    }


@respx.mock
def test_send_template_with_components_document_header():
    """Body + document header -- header component precedes body."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.doc"}]})
    )
    wamid = _make_client().send_template_with_components(
        to="+919999999999",
        name="order_judgment_v1",
        language="en_US",
        body_variables=["Smith vs Bank", "12 May 2026"],
        header_media_id="media-789",
    )
    assert wamid == "wamid.doc"
    components = _captured_body(route)["template"]["components"]
    assert len(components) == 2
    assert components[0] == {
        "type": "header",
        "parameters": [{"type": "document", "document": {"id": "media-789"}}],
    }
    assert components[1]["type"] == "body"
    assert components[1]["parameters"] == [
        {"type": "text", "text": "Smith vs Bank"},
        {"type": "text", "text": "12 May 2026"},
    ]


@respx.mock
def test_send_template_with_components_url_button():
    """Body + URL button -- button component appended after body."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.btn"}]})
    )
    wamid = _make_client().send_template_with_components(
        to="+919999999999",
        name="nowlez_new_order_v1",
        language="en_US",
        body_variables=["Order #123"],
        button_url_variables=["abc-token-xyz"],
    )
    assert wamid == "wamid.btn"
    components = _captured_body(route)["template"]["components"]
    assert len(components) == 2
    assert components[0]["type"] == "body"
    assert components[1] == {
        "type": "button",
        "sub_type": "url",
        "index": "0",
        "parameters": [{"type": "text", "text": "abc-token-xyz"}],
    }


@respx.mock
def test_send_template_with_components_full_new_order_shape():
    """Body + document header + URL button -- the full nowlez_new_order_v1 shape."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.full"}]})
    )
    wamid = _make_client().send_template_with_components(
        to="+919876543210",
        name="nowlez_new_order_v1",
        language="en_US",
        body_variables=["Smith vs Bank", "Order Dated 12-May-2026"],
        header_media_id="media-pdf-001",
        button_url_variables=["order-token-xyz"],
    )
    assert wamid == "wamid.full"
    body = _captured_body(route)
    assert body["to"] == "919876543210"  # leading + stripped
    template = body["template"]
    assert template["name"] == "nowlez_new_order_v1"
    assert template["language"] == {"code": "en_US"}
    components = template["components"]
    assert len(components) == 3
    # Order matters: header, body, button.
    assert components[0]["type"] == "header"
    assert components[0]["parameters"][0] == {
        "type": "document",
        "document": {"id": "media-pdf-001"},
    }
    assert components[1]["type"] == "body"
    assert components[1]["parameters"] == [
        {"type": "text", "text": "Smith vs Bank"},
        {"type": "text", "text": "Order Dated 12-May-2026"},
    ]
    assert components[2] == {
        "type": "button",
        "sub_type": "url",
        "index": "0",
        "parameters": [{"type": "text", "text": "order-token-xyz"}],
    }


@respx.mock
def test_send_template_with_components_5xx_raises_transient():
    respx.post(_MESSAGES_URL).mock(return_value=Response(503, text="boom"))
    with pytest.raises(MetaTransientError):
        _make_client().send_template_with_components(
            to="+919999999999",
            name="nowlez_new_order_v1",
            language="en_US",
            body_variables=["X"],
        )


@respx.mock
def test_send_template_with_components_24h_window_raises():
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        _make_client().send_template_with_components(
            to="+919999999999",
            name="nowlez_new_order_v1",
            language="en_US",
            body_variables=["X"],
        )


@respx.mock
def test_send_template_with_document_legacy_method_still_works():
    """The pre-existing send_template_with_document helper is preserved."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.legacy"}]})
    )
    wamid = _make_client().send_template_with_document(
        to="+919999999999",
        name="order_judgment_v1",
        language="en_US",
        variables=["Smith vs Bank"],
        document_media_id="media-42",
    )
    assert wamid == "wamid.legacy"
    components = _captured_body(route)["template"]["components"]
    assert components[0]["type"] == "header"
    assert components[0]["parameters"][0]["document"] == {"id": "media-42"}
    assert components[1]["type"] == "body"


# ---------------------------------------------------------------------------
# META_TEMPLATES_FALLBACK_TO_TEXT — dev-mode bypass of the template path.
# When set, every send_template* call falls through to MetaClient.send_text
# with a stub body. Useful in local dev / CI where templates aren't filed yet.
# ---------------------------------------------------------------------------


@respx.mock
def test_send_template_fallback_to_text_when_env_set(monkeypatch):
    """``META_TEMPLATES_FALLBACK_TO_TEXT=1`` reroutes send_template -> send_text."""
    monkeypatch.setenv("META_TEMPLATES_FALLBACK_TO_TEXT", "1")
    import json

    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.fall1"}]})
    )
    wamid = _make_client().send_template(
        to="+919999999999",
        name="welcome_v1",
        language="en_US",
        variables=["Alice"],
    )
    assert wamid == "wamid.fall1"

    body = json.loads(route.calls.last.request.content.decode())
    # send_text payload type=text, not type=template.
    assert body["type"] == "text"
    assert "welcome_v1" in body["text"]["body"]
    assert "Alice" in body["text"]["body"]


@respx.mock
def test_send_template_with_document_fallback_to_text(monkeypatch):
    """The legacy with_document helper also honors the dev-mode env var."""
    monkeypatch.setenv("META_TEMPLATES_FALLBACK_TO_TEXT", "1")
    import json

    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.fall2"}]})
    )
    wamid = _make_client().send_template_with_document(
        to="+919999999999",
        name="order_judgment_v1",
        language="en_US",
        variables=["Smith"],
        document_media_id="media-fallback",
    )
    assert wamid == "wamid.fall2"

    body = json.loads(route.calls.last.request.content.decode())
    assert body["type"] == "text"
    # The PDF stub marker is in the rendered text so dev-mode users can
    # see what would have been the document.
    assert "PDF stub: media-fallback" in body["text"]["body"]


@respx.mock
def test_send_template_with_components_fallback_to_text(monkeypatch):
    """send_template_with_components also honors the dev-mode env var, and
    includes both the header media-id stub and the button URL extras."""
    monkeypatch.setenv("META_TEMPLATES_FALLBACK_TO_TEXT", "1")
    import json

    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.fall3"}]})
    )
    wamid = _make_client().send_template_with_components(
        to="+919999999999",
        name="nowlez_new_order_v1",
        language="en_US",
        body_variables=["Smith vs Bank"],
        header_media_id="media-h",
        button_url_variables=["tok-1"],
    )
    assert wamid == "wamid.fall3"

    body = json.loads(route.calls.last.request.content.decode())
    text = body["text"]["body"]
    assert body["type"] == "text"
    assert "nowlez_new_order_v1" in text
    assert "PDF: media-h" in text
    assert "link: tok-1" in text
