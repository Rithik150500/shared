"""Tests for SMS OTP delivery via MSG91.

Uses `respx` to mock httpx HTTP calls (the `responses` library only mocks
the `requests` library, not `httpx`).
"""
import httpx
import pytest
import respx

from identity.delivery.sms import send_otp_sms
from identity.errors import DeliveryFailed


@respx.mock
def test_send_otp_sms_success():
    route = respx.post("https://api.msg91.com/api/v5/otp").mock(
        return_value=httpx.Response(200, json={"type": "success", "request_id": "REQ123"})
    )
    rid = send_otp_sms(phone="+919876543210", code="123456", auth_key="key", template_id="TPL")
    assert rid == "REQ123"
    assert route.called


@respx.mock
def test_send_otp_sms_msg91_error_response():
    respx.post("https://api.msg91.com/api/v5/otp").mock(
        return_value=httpx.Response(400, json={"type": "error", "message": "Invalid template"})
    )
    with pytest.raises(DeliveryFailed, match="sms"):
        send_otp_sms(phone="+919876543210", code="123456", auth_key="key", template_id="TPL")


@respx.mock
def test_send_otp_sms_non_success_type():
    """MSG91 may return 200 but type != 'success' — still a failure."""
    respx.post("https://api.msg91.com/api/v5/otp").mock(
        return_value=httpx.Response(200, json={"type": "error", "message": "Quota exceeded"})
    )
    with pytest.raises(DeliveryFailed, match="sms"):
        send_otp_sms(phone="+919876543210", code="123456", auth_key="key", template_id="TPL")


@respx.mock
def test_send_otp_sms_strips_plus_from_phone():
    """MSG91 expects mobile without leading +."""
    route = respx.post("https://api.msg91.com/api/v5/otp").mock(
        return_value=httpx.Response(200, json={"type": "success", "request_id": "X"})
    )
    send_otp_sms(phone="+919876543210", code="123456", auth_key="key", template_id="TPL")
    # MSG91 uses query params; check the URL has the stripped phone
    call = route.calls[0]
    assert "mobile=919876543210" in str(call.request.url)


@respx.mock
def test_send_otp_sms_passes_template_and_authkey():
    route = respx.post("https://api.msg91.com/api/v5/otp").mock(
        return_value=httpx.Response(200, json={"type": "success", "request_id": "X"})
    )
    send_otp_sms(phone="+919876543210", code="123456", auth_key="my-key", template_id="TPL-99")
    url = str(route.calls[0].request.url)
    assert "authkey=my-key" in url
    assert "template_id=TPL-99" in url
    assert "otp=123456" in url


@respx.mock
def test_send_otp_sms_network_error():
    respx.post("https://api.msg91.com/api/v5/otp").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DeliveryFailed):
        send_otp_sms(phone="+919876543210", code="123456", auth_key="key", template_id="TPL")
