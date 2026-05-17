"""Env-var-driven configuration. Mirrors Nowlez's existing ECOURTS_* prefix."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ECourtsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    ecourts_district_base_url: str = "https://app.ecourts.gov.in/ecourt_mobile_DC/"
    ecourts_hc_base_url: str = "https://app.ecourts.gov.in/ecourt_mobile_HC/"
    ecourts_user_agent: str = "eCourts-Bot/0.1 (+https://nowlez.in/contact)"

    ecourts_max_concurrency: int = 10
    ecourts_circuit_failure_threshold: int = 5
    ecourts_circuit_recovery_timeout_seconds: int = 60
    ecourts_circuit_half_open_max_calls: int = 3
    ecourts_alert_webhook_url: str | None = None

    ecourts_health_poll_interval_seconds: int = 30
    ecourts_health_endpoint: str = "appReleaseWebService.php"

    ecourts_retry_max_attempts: int = 3
    ecourts_retry_base_delay_seconds: float = 1.0
