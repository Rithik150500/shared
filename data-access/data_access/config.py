"""Data access configuration.

Production discipline (May 2026 audit follow-up): the ``DATABASE_URL``
default points at ``localhost``, which works for local dev + the
docker-compose deploy on case-tracker but silently failed on Railway for
weeks. Casepilot ran with ``DATABASE_URL`` unset, the scheduler crons
crashed daily with ``Connection refused``, and the gap surfaced only
during an unrelated Redis investigation.

To prevent the same class of bug, this module now **hard-fails at
import time** if the default URL is still active AND a production
environment is detected (via ``RAILWAY_ENVIRONMENT`` / ``IS_PRODUCTION`` /
``ENV=production``). In non-prod (no indicator), the default still works
but is logged as INFO so it's visible if anyone is paying attention.
"""
from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


_DEV_DEFAULT_DATABASE_URL = "postgresql+psycopg2://localhost/nowlez_munshi_shared"

# Env vars whose presence we treat as "this is a production environment".
# RAILWAY_ENVIRONMENT and RAILWAY_PROJECT_ID are auto-injected by Railway.
# IS_PRODUCTION and ENV=production are conventional manual overrides.
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


class DataAccessSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = _DEV_DEFAULT_DATABASE_URL
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE_SECONDS: int = 3600

    def model_post_init(self, __context) -> None:
        """Hard-fail in prod if DATABASE_URL is still the dev default."""
        if self.DATABASE_URL != _DEV_DEFAULT_DATABASE_URL:
            return  # Operator explicitly set it; no further action needed.
        if _is_production_env():
            raise RuntimeError(
                "DATABASE_URL is unset (defaulted to localhost) but a "
                "production environment was detected via one of "
                f"{('ENV=production', *_PROD_INDICATOR_ENV_VARS)}. "
                "Set DATABASE_URL in the env-file or Railway service "
                "variables. See data_access.config module docstring for "
                "the May 2026 incident context."
            )
        logger.info(
            "DATABASE_URL using dev default (localhost). Set DATABASE_URL "
            "env var for non-local use."
        )


settings = DataAccessSettings()
