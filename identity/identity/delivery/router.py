"""OTP delivery router: WhatsApp first, SMS fallback.

The user-facing experience is that the code arrives via WhatsApp ~95% of the
time (cheaper, more reliable). When Meta is down / template not approved /
quota exceeded, we transparently fall back to MSG91 SMS so the user still
receives their code.
"""
from __future__ import annotations

import logging

from ..errors import DeliveryFailed
from .email import send_otp_email
from .sms import send_otp_sms
from .whatsapp import send_otp_whatsapp

logger = logging.getLogger(__name__)


def deliver_otp(phone: str, code: str) -> tuple[str, str]:
    """Try WhatsApp; on DeliveryFailed, fall back to SMS.

    Returns (channel, provider_id) where channel is "whatsapp" or "sms" and
    provider_id is Meta's wamid or MSG91's request_id.

    Raises DeliveryFailed if both channels fail; the raised exception carries
    the SMS-side details (the last attempt).
    """
    try:
        msg_id = send_otp_whatsapp(phone, code)
        return ("whatsapp", msg_id)
    except DeliveryFailed as wa_err:
        logger.warning(
            "WhatsApp send failed; falling back to SMS: %s", wa_err.provider_error
        )
        try:
            req_id = send_otp_sms(phone, code)
            return ("sms", req_id)
        except DeliveryFailed:
            logger.error("Both WhatsApp and SMS delivery failed for OTP send")
            raise


def deliver_email_otp(email: str, code: str) -> tuple[str, str]:
    """Deliver an OTP via email (Resend). No fallback — email is its own channel.

    Returns (channel='email', provider_id). Raises EmailDeliveryFailed on failure.
    """
    return send_otp_email(email, code)
