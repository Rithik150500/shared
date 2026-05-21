"""Tests for WhatsApp OTP delivery via the shared ``whatsapp_delivery`` package.

After audit D-12, ``identity.delivery.whatsapp`` is a thin adapter over
``whatsapp_delivery.TemplateClient.send_template_with_components``. These
tests assert the wire-level payload (so a future refactor inside
``whatsapp_delivery`` can't silently break OTP delivery) and the
DeliveryFailed-wrapping semantics that the delivery router relies on.

Uses ``respx`` to mock httpx HTTP calls (the ``responses`` library only
mocks the ``requests`` library, not ``httpx``).
"""
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from identity.delivery.whatsapp import send_otp_whatsapp
from identity.errors import DeliveryFailed


# D-12: after the refactor the Graph API version is owned by
# ``whatsapp_delivery.meta_client.META_GRAPH_API_VERSION`` (v20.0 at the
# time of this commit). The OTP path now picks it up automatically; pre-
# D-12 this URL was pinned to v18.0 in identity's own code.
_MESSAGES_URL = "https://graph.facebook.com/v20.0/PHONE_ID/messages"


@respx.mock
def test_send_otp_whatsapp_success():
    respx.post(_MESSAGES_URL).mock(
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
    respx.post(_MESSAGES_URL).mock(
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
    route = respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
    )
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
    route = respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
    )
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
def test_send_otp_whatsapp_payload_carries_otp_in_body_and_button():
    """The auth template needs the OTP code in BOTH the body slot and the
    Copy-code URL button slot. Verify both are present."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
    )
    send_otp_whatsapp(
        phone="+919876543210",
        code="654321",
        phone_number_id="PHONE_ID",
        access_token="tok",
    )
    payload = json.loads(route.calls[0].request.content)
    components = payload["template"]["components"]
    # Components: body + button (no header). Order matters for Meta.
    body_component = next(c for c in components if c["type"] == "body")
    button_component = next(c for c in components if c["type"] == "button")
    assert body_component["parameters"] == [{"type": "text", "text": "654321"}]
    assert button_component["sub_type"] == "url"
    assert button_component["index"] == "0"
    assert button_component["parameters"] == [{"type": "text", "text": "654321"}]


@respx.mock
def test_send_otp_whatsapp_passes_bearer_token():
    route = respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
    )
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
    # Mock raises ConnectError -> wrapped as DeliveryFailed (so the
    # delivery router still falls back to SMS cleanly).
    respx.post(_MESSAGES_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(DeliveryFailed):
        send_otp_whatsapp(
            phone="+919876543210",
            code="123456",
            phone_number_id="PHONE_ID",
            access_token="tok",
        )


@respx.mock
def test_send_otp_whatsapp_429_rate_limit_wrapped_as_delivery_failed():
    """A Meta 429 surfaces inside whatsapp_delivery as MetaTransientError;
    the OTP adapter must re-wrap it as DeliveryFailed so the router can
    fall back to SMS instead of leaking a whatsapp_delivery-specific
    exception type into identity callers."""
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(429, text="too many requests")
    )
    with pytest.raises(DeliveryFailed) as exc_info:
        send_otp_whatsapp(
            phone="+919876543210",
            code="123456",
            phone_number_id="PHONE_ID",
            access_token="tok",
        )
    assert exc_info.value.channel == "whatsapp"


@respx.mock
def test_send_otp_whatsapp_5xx_wrapped_as_delivery_failed():
    """5xx (Meta outage / temporary) is still surfaced as DeliveryFailed
    so the SMS fallback fires."""
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(503, text="upstream"))
    with pytest.raises(DeliveryFailed) as exc_info:
        send_otp_whatsapp(
            phone="+919876543210",
            code="123456",
            phone_number_id="PHONE_ID",
            access_token="tok",
        )
    assert exc_info.value.channel == "whatsapp"


def test_send_otp_whatsapp_routes_through_template_client():
    """D-12: integration check that the OTP path delegates to
    ``whatsapp_delivery.TemplateClient.send_template_with_components``
    rather than re-implementing the Meta send. If a future refactor
    re-introduces a direct httpx call inside identity, this test breaks
    -- which is exactly the duplication we just removed."""
    with patch(
        "identity.delivery.whatsapp.TemplateClient.send_template_with_components",
        return_value="wamid.via-shared",
    ) as send_mock:
        result = send_otp_whatsapp(
            phone="+919876543210",
            code="987654",
            phone_number_id="PHONE_ID",
            access_token="tok",
            timeout_seconds=12.5,
        )
    assert result == "wamid.via-shared"
    send_mock.assert_called_once()
    kwargs = send_mock.call_args.kwargs
    # The OTP code is passed as BOTH the body variable and the URL-button
    # variable -- the auth template shape.
    assert kwargs["body_variables"] == ["987654"]
    assert kwargs["button_url_variables"] == ["987654"]
    assert kwargs["name"] == "auth_otp_v1"
    assert kwargs["language"] == "en"
    # Identity's timeout knob threads through to whatsapp_delivery.
    assert kwargs["timeout_seconds"] == 12.5
