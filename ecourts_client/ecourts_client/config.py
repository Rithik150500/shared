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
    # NOTE: ``ecourts_circuit_half_open_max_calls`` was removed here. It had ZERO
    # readers anywhere in shared/, casepilot/ or ecourts-bot/ -- it read as an
    # implemented knob but did nothing. It is also the wrong idea: admitting N
    # concurrent probes multiplies traffic against a host that bans by IP, which
    # is what the cascade guard exists to prevent. ``extra="ignore"`` above means
    # a stale ECOURTS_CIRCUIT_HALF_OPEN_MAX_CALLS left in any droplet .env is
    # silently ignored rather than crashing startup.
    #
    # Ceiling on the doubling half-open ladder. Previously an unreachable
    # hardcoded 1800.0 inside CircuitBreaker: the constructor took it, but no
    # decorator, registry call or policy forwarded it, so production could never
    # set it and a breaker that kept failing its probe went quiet for 30 minutes.
    # 300s saturates at rung 3 (60 -> 120 -> 240 -> 300) and renders to users as
    # "about 5 minutes" instead of "about 30 minutes".
    ecourts_circuit_max_recovery_timeout_seconds: float = 300.0
    # Fraction by which a recovery window may be randomly SHORTENED. A fully
    # deterministic ladder lets a caller on a fixed cadence arrive on the re-arm
    # instant every time and win the single half-open probe on every rung --
    # observed on prod 2026-07-29, where a 60s-interval cron walked this breaker
    # 60 -> 120 -> 240 -> 480s and held it open for interactive users. Set to 0.0
    # to restore the previous exactly-deterministic behaviour.
    ecourts_circuit_recovery_jitter: float = 0.2
    ecourts_alert_webhook_url: str | None = None

    ecourts_health_poll_interval_seconds: int = 30
    ecourts_health_endpoint: str = "appReleaseWebService.php"

    ecourts_retry_max_attempts: int = 3
    ecourts_retry_base_delay_seconds: float = 1.0

    # Failure taxonomy (ECOURTS_FAILURE_TAXONOMY). OFF by default -- flipping it
    # on is the whole rollout. When False the circuit breakers keep their
    # historical behaviour of counting EVERY exception as an availability
    # failure, which means client-side errors open them: five user-typed bad
    # CNRs raise CNRNotFound five times and trip the process-wide breaker for
    # every tenant. When True only exceptions that
    # resilience.failure_policy.classify_failure calls an availability signal
    # are counted; client-side and content errors are ignored.
    ecourts_failure_taxonomy: bool = False

    # Per-court circuit breakers (ECOURTS_PER_COURT_CIRCUIT). OFF by default.
    # Requires the failure taxonomy: without it every error is TRIP_GLOBAL and
    # there is nothing to route to a court breaker. When ON, each call consults
    # the global breaker AND a court-keyed one (dc:<state> / hc:<code>), so one
    # court going down no longer blocks every other court.
    ecourts_per_court_circuit: bool = False
    ecourts_court_failure_threshold: int = 5
    # Longer than the global 60s: a court that is genuinely down stays down for
    # a while, and probing it spends the shared per-IP request budget.
    ecourts_court_recovery_timeout_seconds: float = 120.0
    # Court breakers count failures in a SLIDING WINDOW. Consecutive counting
    # cannot trip a coarse state-level key, because a partial outage yields an
    # interleaved success/failure stream that never reaches N in a row.
    ecourts_court_failure_window_seconds: float = 300.0
    # Court ladders start from a 120s base, so they get a higher ceiling than the
    # global breaker's. Same defect as the global one: previously unreachable.
    ecourts_court_max_recovery_timeout_seconds: float = 600.0
    # When this many court breakers are open at once, force the global breaker
    # open so a broad outage collapses onto one breaker instead of N probing
    # half-open ladders against an IP that bans on burst. 0 disables.
    ecourts_cascade_open_court_threshold: int = 8

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

    # Tier-2 distributed rate limiter (ECOURTS_USE_REDIS_LIMITER). When ON, the
    # _get_rate_gate() factory returns a Redis-backed GCRA limiter that caps the
    # AGGREGATE egress rate across all processes on the one IP (the per-process
    # interval above becomes the limiter's base + fail-open floor). Default OFF:
    # behavior is byte-identical to the per-process _RateGate. See
    # resilience/redis_limiter.py + docs/audit-finding-ecourts-ipwide-throttle.md.
    ecourts_use_redis_limiter: bool = False
    ecourts_redis_limiter_widen_factor: float = 2.0        # AIMD multiplier on a 405
    ecourts_redis_limiter_max_interval_seconds: float = 8.0
    ecourts_redis_limiter_penalty_ttl_seconds: int = 300   # widened interval auto-resets after this
