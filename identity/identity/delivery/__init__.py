"""Delivery channels for OTP codes.

Currently exposes WhatsApp (Meta Cloud API). SMS (MSG91) and the unified
delivery router land in later tasks.
"""
from .whatsapp import send_otp_whatsapp

__all__ = ["send_otp_whatsapp"]
