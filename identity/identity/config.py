"""Identity package configuration.

Values come from environment variables (via pydantic-settings) with sensible
defaults for local development. Production deployments must override secrets.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class IdentitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # JWT
    JWT_SECRET_KEY: str = "change-me-in-prod"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TTL_MINUTES: int = 30

    # Refresh tokens (opaque, SHA-256-hashed)
    REFRESH_TTL_DAYS: int = 30

    # OTP
    OTP_LENGTH: int = 6
    OTP_TTL_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 3

    # Rate limits (per spec § 4.7)
    OTP_PER_PHONE_PER_HOUR: int = 5
    OTP_PER_IP_PER_HOUR: int = 20
    PWD_FAIL_PER_PHONE_PER_15MIN: int = 5
    PWD_FAIL_PER_IP_PER_15MIN: int = 50

    # Argon2id (OWASP 2026 defaults — profile under load)
    ARGON2_TIME_COST: int = 2
    ARGON2_MEMORY_COST_KB: int = 65536  # 64 MB
    ARGON2_PARALLELISM: int = 4

    # Delivery — WhatsApp (Meta Cloud API)
    META_ACCESS_TOKEN: str = ""
    META_PHONE_NUMBER_ID: str = ""
    META_AUTH_TEMPLATE_NAME: str = "auth_otp_v1"

    # Delivery — SMS (MSG91)
    MSG91_AUTH_KEY: str = ""
    MSG91_OTP_TEMPLATE_ID: str = ""
    MSG91_SENDER_ID: str = "NOWLEZ"

    # Delivery timeouts
    WHATSAPP_SEND_TIMEOUT_SECONDS: float = 15.0
    SMS_SEND_TIMEOUT_SECONDS: float = 10.0

    # WhatsApp one-tap login (Method A)
    WA_LOGIN_NONCE_TTL_WEB2BOT_SECONDS: int = 300
    WA_LOGIN_NONCE_TTL_BOT2WEB_SECONDS: int = 120
    WA_LOGIN_NONCE_LENGTH: int = 24  # token_urlsafe byte count; >=128 bits
    MUNSHI_WA_DIGITS: str = "919643460175"  # wa.me target = Munshi BOT number (NOT casepilot OTP)
    WEB_DASHBOARD_BASE_URL: str = ""  # e.g. https://app.nowlez.com (bot2web deep-link base)

    # Email OTP (Method C) — Resend
    EMAIL_OTP_FROM: str = ""  # e.g. 'Nowlez <noreply@nowlez.com>'
    RESEND_API_KEY: str = ""  # shared with casepilot RESEND_API_KEY
    EMAIL_SEND_TIMEOUT_SECONDS: int = 10
    OTP_PER_EMAIL_PER_HOUR: int = 5  # mirrors OTP_PER_PHONE_PER_HOUR


settings = IdentitySettings()
