"""Tests for whatsapp_delivery.meta_client.MetaClient.

Uses respx to mock the Meta Graph API at the httpx layer.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
)
from whatsapp_delivery.meta_client import MetaClient


def _make_client() -> MetaClient:
    return MetaClient(phone_number_id="111", access_token="tok")


@respx.mock
def test_send_text_returns_wamid():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.abc"}]})
    )
    assert _make_client().send_text("+919999999999", "hello") == "wamid.abc"


@respx.mock
def test_send_text_5xx_raises_transient():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(503, text="server error")
    )
    with pytest.raises(MetaTransientError):
        _make_client().send_text("+919999999999", "hello")


@respx.mock
def test_send_text_24h_window_raises():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        _make_client().send_text("+919999999999", "hello")


@respx.mock
def test_send_text_other_4xx_raises_invalid():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 12345, "message": "bad"}}
        )
    )
    with pytest.raises(MetaInvalidMessage):
        _make_client().send_text("+919999999999", "hello")
