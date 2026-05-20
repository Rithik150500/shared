"""WhatsApp delivery configuration (loaded from env).

Production discipline (May 2026 audit follow-up): ``shared_redis_url``
defaults to ``redis://redis:6379/0`` (the docker-compose service-name
pattern). On Railway there's no host called "redis", so the connection
silently fails — for weeks, casepilot's ``enqueue_send_template`` calls
went into the void with no consumer. To prevent the same class of bug
this module now hard-fails at instantiation if the default URL is still
active AND a production environment is detected.

See ``data_access.config`` for the same pattern applied to
``DATABASE_URL`` — both were tripped by the same incident.
"""
from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


_DEV_DEFAULT_REDIS_URL = "redis://redis:6379/0"

_PROD_INDICATOR_ENV_VARS: tuple[str, ...] = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "IS_PRODUCTION",
)


def _is_production_env() -> bool:
    """True iff any common production indicator env var is set + non-empty."""
    if os.environ.get("ENV", "").strip().lower() == "production":
        return True
    return any(
        (os.environ.get(k) or "").strip()
        for k in _PROD_INDICATOR_ENV_VARS
    )


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
    shared_redis_url: str = _DEV_DEFAULT_REDIS_URL

    # Munshi → Nowlez bridge
    nowlez_internal_url: str | None = None
    nowlez_internal_token: str | None = None

    # Idempotency dedup window
    idempotency_window_seconds: int = 86400 * 7

    # Rate limiting
    new_order_throttle_per_24h: int = 3

    # Kill switch
    whatsapp_nowlez_disabled: bool = False

    def model_post_init(self, __context) -> None:
        """Hard-fail in prod if shared_redis_url is still the dev default."""
        if self.shared_redis_url != _DEV_DEFAULT_REDIS_URL:
            return
        if _is_production_env():
            raise RuntimeError(
                "shared_redis_url is unset (defaulted to "
                f"{_DEV_DEFAULT_REDIS_URL}) but a production environment "
                "was detected via one of "
                f"{('ENV=production', *_PROD_INDICATOR_ENV_VARS)}. "
                "Set SHARED_REDIS_URL in the env-file or Railway service "
                "variables. See whatsapp_delivery.config module docstring "
                "for the May 2026 incident context."
            )
        logger.info(
            "shared_redis_url using dev default (%s). Set SHARED_REDIS_URL "
            "env var for non-local use.", _DEV_DEFAULT_REDIS_URL,
        )
