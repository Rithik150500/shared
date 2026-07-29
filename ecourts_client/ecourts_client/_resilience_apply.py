"""Applies the sync resilience stack to picker/search/cause-list methods
on DistrictCourtClient and HighCourtClient.

This module is the SINGLE SOURCE OF TRUTH for "which methods are protected".
Adding a new method to a client class means adding one entry here.

WHY fetch_case / fetch_pdf are excluded:
  They're already wrapped by the async stack in client.py via
  `_wrap_with_resilience(_fetch_case_async)`. Wrapping the sync version
  too would stack two retry layers (the 3x3 multiplicative behavior the
  resilience refactor was designed to avoid). Direct sync calls to
  DistrictCourtClient.fetch_case() / .fetch_pdf() bypass resilience by
  design -- callers should use the top-level `ecourts_client.fetch_case`.

WHY a separate module (and not just put it in __init__.py):
  - Tests can introspect the registry directly without triggering
    apply-at-import side effects.
  - The "what's protected" list is greppable in one place.
"""
from __future__ import annotations

from functools import wraps

from ecourts_client.config import ECourtsConfig
from ecourts_client.resilience import (
    with_circuit_breaker_sync,
    with_retry_sync,
    with_semaphore_sync,
)


_DO_NOT_WRAP = frozenset({
    ("DistrictCourtClient", "fetch_case"),
    ("DistrictCourtClient", "fetch_pdf"),
    ("HighCourtClient", "fetch_case"),
    ("HighCourtClient", "fetch_pdf"),
})


_WRAP_REGISTRY = [
    # DistrictCourtClient -- 10 methods
    ("DistrictCourtClient", "list_states"),
    ("DistrictCourtClient", "list_districts"),
    ("DistrictCourtClient", "list_court_complexes"),
    ("DistrictCourtClient", "list_police_stations"),
    ("DistrictCourtClient", "list_case_types"),
    ("DistrictCourtClient", "search_by_party_name"),
    ("DistrictCourtClient", "search_by_case_number"),
    ("DistrictCourtClient", "search_by_fir"),
    ("DistrictCourtClient", "fetch_daily_business"),
    ("DistrictCourtClient", "fetch_cause_list"),
    # HighCourtClient -- 9 methods
    ("HighCourtClient", "list_states"),
    ("HighCourtClient", "list_bench_sittings"),
    ("HighCourtClient", "list_hc_benches"),
    ("HighCourtClient", "list_case_types"),
    ("HighCourtClient", "search_by_party_name"),
    ("HighCourtClient", "search_by_case_number"),
    ("HighCourtClient", "fetch_daily_business"),
    ("HighCourtClient", "fetch_cause_list_index"),
    ("HighCourtClient", "fetch_cause_list_pdf_rows"),
]


# Marker attribute used to detect "already wrapped" so apply_sync_resilience()
# is idempotent. Stored on the wrapped function, not the class.
_RESILIENCE_MARKER = "_ecourts_sync_resilience_applied"


def _build_wrapped(raw_fn, *, config: ECourtsConfig):
    """Compose the three sync decorators in the same order the async stack
    composes them in client.py: semaphore (outermost) -> circuit breaker
    -> retry (innermost, closest to the call)."""
    wrapped = with_retry_sync(
        max_attempts=config.ecourts_retry_max_attempts,
        base_delay=config.ecourts_retry_base_delay_seconds,
    )(raw_fn)
    wrapped = with_circuit_breaker_sync(
        name="ecourts_global",
        failure_threshold=config.ecourts_circuit_failure_threshold,
        recovery_timeout=config.ecourts_circuit_recovery_timeout_seconds,
        use_taxonomy=config.ecourts_failure_taxonomy,
        max_recovery_timeout=config.ecourts_circuit_max_recovery_timeout_seconds,
        jitter=config.ecourts_circuit_recovery_jitter,
    )(wrapped)
    wrapped = with_semaphore_sync(
        name="ecourts_global",
        max_concurrency=config.ecourts_max_concurrency,
    )(wrapped)
    # Preserve original for tests + introspection.
    @wraps(raw_fn)
    def applied(*args, **kwargs):
        return wrapped(*args, **kwargs)
    setattr(applied, _RESILIENCE_MARKER, True)
    setattr(applied, "__wrapped__", raw_fn)
    return applied


def apply_sync_resilience(*, config: ECourtsConfig | None = None) -> None:
    """Decorate every method in `_WRAP_REGISTRY` on its target class with
    the sync resilience stack. Idempotent -- a second call is a no-op.

    Must be called AFTER both client modules (district, highcourt) have
    been imported. ecourts_client/__init__.py handles this by calling
    apply_sync_resilience() after importing both classes.
    """
    if config is None:
        config = ECourtsConfig()

    from ecourts_client.district import DistrictCourtClient
    from ecourts_client.highcourt import HighCourtClient
    classes = {"DistrictCourtClient": DistrictCourtClient, "HighCourtClient": HighCourtClient}

    for class_name, method_name in _WRAP_REGISTRY:
        if (class_name, method_name) in _DO_NOT_WRAP:
            # Defensive: should never happen given the lists are disjoint by
            # construction, but if someone edits the registry wrong, fail loud.
            raise RuntimeError(
                f"({class_name}, {method_name}) is in both _WRAP_REGISTRY and "
                f"_DO_NOT_WRAP -- this is a bug in _resilience_apply.py"
            )
        cls = classes[class_name]
        current = getattr(cls, method_name)
        if getattr(current, _RESILIENCE_MARKER, False):
            continue  # already wrapped -- idempotency
        wrapped = _build_wrapped(current, config=config)
        setattr(cls, method_name, wrapped)
