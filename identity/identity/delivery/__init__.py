"""Delivery channels for OTP codes.

Exposes the WhatsApp (Meta Cloud API) and SMS (MSG91) senders. The unified
delivery router lands in a later task.
"""
from .sms import send_otp_sms
from .whatsapp import send_otp_whatsapp

__all__ = ["send_otp_whatsapp", "send_otp_sms"]
