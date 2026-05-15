from unittest.mock import patch

import pytest

from identity.delivery.router import deliver_otp
from identity.errors import DeliveryFailed


def test_whatsapp_success_returns_whatsapp_channel():
    with patch("identity.delivery.router.send_otp_whatsapp", return_value="wamid.X"):
        ch, pid = deliver_otp(phone="+919876543210", code="123456")
        assert ch == "whatsapp"
        assert pid == "wamid.X"


def test_whatsapp_failure_falls_back_to_sms():
    with patch("identity.delivery.router.send_otp_whatsapp", side_effect=DeliveryFailed("whatsapp", "timeout")):
        with patch("identity.delivery.router.send_otp_sms", return_value="REQ123"):
            ch, pid = deliver_otp(phone="+919876543210", code="123456")
            assert ch == "sms"
            assert pid == "REQ123"


def test_both_fail_raises_delivery_failed():
    with patch("identity.delivery.router.send_otp_whatsapp", side_effect=DeliveryFailed("whatsapp", "x")):
        with patch("identity.delivery.router.send_otp_sms", side_effect=DeliveryFailed("sms", "y")):
            with pytest.raises(DeliveryFailed):
                deliver_otp(phone="+919876543210", code="123456")


def test_whatsapp_succeeds_sms_not_called():
    """When WhatsApp succeeds, SMS must NOT be invoked (cost optimization)."""
    with patch("identity.delivery.router.send_otp_whatsapp", return_value="wamid.X") as wa_mock:
        with patch("identity.delivery.router.send_otp_sms") as sms_mock:
            deliver_otp(phone="+919876543210", code="123456")
            assert wa_mock.called
            assert not sms_mock.called


def test_failed_whatsapp_then_failed_sms_preserves_sms_error():
    """The DeliveryFailed raised at end is for the SMS attempt (the last try)."""
    with patch("identity.delivery.router.send_otp_whatsapp", side_effect=DeliveryFailed("whatsapp", "wa-err")):
        with patch("identity.delivery.router.send_otp_sms", side_effect=DeliveryFailed("sms", "sms-err")):
            with pytest.raises(DeliveryFailed) as exc_info:
                deliver_otp(phone="+919876543210", code="123456")
            assert exc_info.value.channel == "sms"
