"""Delivery channels for OTP codes.

Exposes the WhatsApp (Meta Cloud API) and SMS (MSG91) senders, plus the
unified ``deliver_otp`` router that tries WhatsApp first and falls back to
SMS on failure.
"""
from .router import deliver_otp
from .sms import send_otp_sms
from .whatsapp import send_otp_whatsapp

__all__ = ["send_otp_whatsapp", "send_otp_sms", "deliver_otp"]
