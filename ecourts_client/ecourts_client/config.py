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

    # Read-through cache TTL for the quasi-static picker/list endpoints (states,
    # districts, complexes, case-types, police stations, HC benches). 24h by
    # default -- these lists change on a year+ scale. Only active when a consumer
    # wires a Redis backend via ecourts_client.cache.set_backend().
    ecourts_cache_ttl_seconds: int = 86400

    # Proactive burst control (opt-in tuning knob; OFF by default). eCourts
    # throttles IP bursts to HTTP 405 + an HTML error page for ~15-30 min
    # (docs/RE_NOTES_v4.md); a bulk add/refresh fires one display_pdf_new.php POST
    # per order and can trip it. Set a positive value to enforce a minimum
    # wall-clock interval process-wide between outbound eCourts HTTP calls (see
    # _session._RateGate) -- e.g. ~0.34s => ~3 req/s, under the burst threshold,
    # adding only ~0.34s to a single interactive search. 0 disables (the default:
    # rely on the reactive circuit breaker + the 405->RateLimited classification,
    # and enable this via ECOURTS_MIN_REQUEST_INTERVAL_SECONDS if throttling recurs).
    ecourts_min_request_interval_seconds: float = 0.0
