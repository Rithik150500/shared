"""Tests for WhatsApp OTP delivery via Meta Cloud API.

Uses `respx` to mock httpx HTTP calls (the `responses` library only mocks
the `requests` library, not `httpx`).
"""
import json

import httpx
import pytest
import respx

from identity.delivery.whatsapp import send_otp_whatsapp
from identity.errors import DeliveryFailed


@respx.mock
def test_send_otp_whatsapp_success():
    respx.post("https://graph.facebook.com/v18.0/PHONE_ID/messages").mock(
        return_value=httpx.Response(
            200, json={"messages": [{"id": "wamid.HBgM12345"}]}
        )
    )
    msg_id = send_otp_whatsapp(
        phone="+919876543210",
        code="123456",
        phone_number_id="PHONE_ID",
        access_token="tok",
    )
    assert msg_id == "wamid.HBgM12345"


@respx.mock
def test_send_otp_whatsapp_meta_error_status():
    respx.post("https://graph.facebook.com/v18.0/PHONE_ID/messages").mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "template not approved"}}
        )
    )
    with pytest.raises(DeliveryFailed, match="whatsapp"):
        send_otp_whatsapp(
            phone="+919876543210",
            code="123456",
            phone_number_id="PHONE_ID",
            access_token="tok",
        )


@respx.mock
def test_send_otp_whatsapp_strips_plus_from_phone():
    """Meta API expects E.164 without the leading +."""
    route = respx.post(
        "https://graph.facebook.com/v18.0/PHONE_ID/messages"
    ).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))
    send_otp_whatsapp(
        phone="+919876543210",
        code="123456",
        phone_number_id="PHONE_ID",
        access_token="tok",
    )
    payload = json.loads(route.calls[0].request.content)
    assert payload["to"] == "919876543210"  # no leading +


@respx.mock
def test_send_otp_whatsapp_uses_auth_template():
    route = respx.post(
        "https://graph.facebook.com/v18.0/PHONE_ID/messages"
    ).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))
    send_otp_whatsapp(
        phone="+919876543210",
        code="123456",
        phone_number_id="PHONE_ID",
        access_token="tok",
    )
    payload = json.loads(route.calls[0].request.content)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "auth_otp_v1"


@respx.mock
def test_send_otp_whatsapp_passes_bearer_token():
    route = respx.post(
        "https://graph.facebook.com/v18.0/PHONE_ID/messages"
    ).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))
    send_otp_whatsapp(
        phone="+919876543210",
        code="123456",
        phone_number_id="PHONE_ID",
        access_token="my-token",
    )
    auth = route.calls[0].request.headers.get("Authorization")
    assert auth == "Bearer my-token"


@respx.mock
def test_send_otp_whatsapp_connection_error():
    # Mock raises ConnectError → wrapped as DeliveryFailed
    respx.post("https://graph.facebook.com/v18.0/PHONE_ID/messages").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(DeliveryFailed):
        send_otp_whatsapp(
            phone="+919876543210",
            code="123456",
            phone_number_id="PHONE_ID",
            access_token="tok",
        )
