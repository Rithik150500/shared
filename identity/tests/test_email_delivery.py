"""Resend email-OTP delivery (sync httpx POST), mocked with respx."""
import httpx
import pytest
import respx

from identity.delivery.email import send_otp_email
from identity.errors import EmailDeliveryFailed


@respx.mock
def test_send_otp_email_success_returns_provider_id():
    route = respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(200, json={"id": "resend-msg-1"})
    )
    channel, pid = send_otp_email(
        "user@example.com", "123456",
        api_key="re_test", from_addr="Nowlez <noreply@nowlez.com>",
    )
    assert channel == "email"
    assert pid == "resend-msg-1"
    assert route.called
    body = route.calls[0].request
    assert b"user@example.com" in body.content
    assert b"123456" in body.content
    assert body.headers["authorization"] == "Bearer re_test"


@respx.mock
def test_send_otp_email_non_2xx_raises():
    respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(422, json={"message": "invalid from"})
    )
    with pytest.raises(EmailDeliveryFailed):
        send_otp_email("u@example.com", "123456", api_key="re_test", from_addr="x@y.com")


@respx.mock
def test_send_otp_email_network_error_raises():
    respx.post("https://api.resend.com/emails").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(EmailDeliveryFailed):
        send_otp_email("u@example.com", "123456", api_key="re_test", from_addr="x@y.com")


def test_deliver_email_otp_router_delegates(monkeypatch):
    from identity.delivery import router
    monkeypatch.setattr(router, "send_otp_email", lambda e, c: ("email", "pid-9"))
    assert router.deliver_email_otp("u@example.com", "123456") == ("email", "pid-9")
