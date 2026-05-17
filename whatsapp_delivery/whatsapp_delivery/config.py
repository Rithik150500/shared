"""WhatsApp delivery configuration (loaded from env)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WhatsAppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Meta WhatsApp Cloud API
    meta_phone_number_id: str
    meta_access_token: str
    meta_verify_token: str
    meta_app_secret: str
    meta_templates_fallback_to_text: bool = False
    meta_waba_id: str | None = None

    # Redis for RQ queue (shared with Munshi)
    shared_redis_url: str = "redis://redis:6379/0"

    # Munshi → Nowlez bridge
    nowlez_internal_url: str | None = None
    nowlez_internal_token: str | None = None

    # Idempotency dedup window
    idempotency_window_seconds: int = 86400 * 7

    # Rate limiting
    new_order_throttle_per_24h: int = 3

    # Kill switch
    whatsapp_nowlez_disabled: bool = False
