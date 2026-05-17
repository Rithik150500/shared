"""Top-level entry points for the shared eCourts client.

`fetch_case` and `fetch_pdf` are wrapped with the layered resilience stack:
    Layer 1: semaphore  (cap concurrency)
    Layer 2: circuit breaker  (fail fast when court site is down)
    Layer 3: retry  (transient transport errors only)
    Layer 4: transport  (synchronous DistrictCourtClient / HighCourtClient,
                         run in a worker thread via asyncio.to_thread)
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ecourts_client.config import ECourtsConfig
from ecourts_client.errors import CNRMalformed
from ecourts_client.models import Case
from ecourts_client.resilience import (
    with_circuit_breaker,
    with_retry,
    with_semaphore,
)
from ecourts_client.routing import classify_cnr


@runtime_checkable
class ECourtsClient(Protocol):
    scope: str

    def fetch_case(self, cnr: str) -> Case: ...
    def fetch_pdf(self, url: str) -> bytes: ...


def get_client_for(cnr: str) -> ECourtsClient:
    scope = classify_cnr(cnr)
    if scope == "district":
        from ecourts_client.district import DistrictCourtClient
        return DistrictCourtClient()
    from ecourts_client.highcourt import HighCourtClient
    return HighCourtClient()


_CONFIG = ECourtsConfig()


def _wrap_with_resilience(fn):
    return with_semaphore(name="ecourts_global", max_concurrency=_CONFIG.ecourts_max_concurrency)(
        with_circuit_breaker(
            name="ecourts_global",
            failure_threshold=_CONFIG.ecourts_circuit_failure_threshold,
            recovery_timeout=_CONFIG.ecourts_circuit_recovery_timeout_seconds,
        )(
            with_retry(
                max_attempts=_CONFIG.ecourts_retry_max_attempts,
                base_delay=_CONFIG.ecourts_retry_base_delay_seconds,
            )(fn)
        )
    )


async def _fetch_case_async(cnr: str) -> Case:
    """Layer 4 thunk: runs the synchronous transport in a worker thread."""
    import asyncio
    return await asyncio.to_thread(get_client_for(cnr).fetch_case, cnr)


async def _fetch_pdf_async(url: str, cnr_hint: str | None = None) -> bytes:
    import asyncio
    if cnr_hint:
        return await asyncio.to_thread(get_client_for(cnr_hint).fetch_pdf, url)
    from ecourts_client.district import DistrictCourtClient
    return await asyncio.to_thread(DistrictCourtClient().fetch_pdf, url)


fetch_case = _wrap_with_resilience(_fetch_case_async)
fetch_pdf = _wrap_with_resilience(_fetch_pdf_async)
