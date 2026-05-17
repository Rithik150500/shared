"""Prometheus metric definitions for the ecourts_client package."""
from __future__ import annotations

try:
    from prometheus_client import Counter, Gauge, Histogram
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


if _AVAILABLE:
    ecourts_fetch_total = Counter(
        "ecourts_fetch_total", "eCourts case fetches", ["result", "court_type"],
    )
    ecourts_fetch_duration_seconds = Histogram(
        "ecourts_fetch_duration_seconds", "eCourts fetch latency", ["court_type"],
    )
    ecourts_circuit_state = Gauge(
        "ecourts_circuit_state", "Circuit state (0=closed,1=half_open,2=open)", ["name"],
    )
    ecourts_circuit_failures_total = Counter(
        "ecourts_circuit_failures_total", "Circuit breaker recorded failures", ["name"],
    )
    ecourts_circuit_state_transitions_total = Counter(
        "ecourts_circuit_state_transitions_total", "Circuit state transitions", ["from", "to"],
    )
    ecourts_health_check_total = Counter(
        "ecourts_health_check_total", "Health probe attempts", ["result"],
    )
    ecourts_pdf_fetch_total = Counter(
        "ecourts_pdf_fetch_total", "PDF fetches", ["result"],
    )
    ecourts_pdf_size_bytes = Histogram(
        "ecourts_pdf_size_bytes", "PDF body size", buckets=(1024, 10240, 102400, 1048576, 10485760),
    )
    ecourts_semaphore_inflight = Gauge(
        "ecourts_semaphore_inflight", "Active in-flight calls under semaphore", ["name"],
    )
    ecourts_semaphore_waiting = Gauge(
        "ecourts_semaphore_waiting", "Calls waiting on semaphore", ["name"],
    )


def setup_sentry_tag() -> None:
    try:
        import sentry_sdk
        sentry_sdk.set_tag("package", "ecourts_client")
    except ImportError:
        pass
