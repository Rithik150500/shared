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
