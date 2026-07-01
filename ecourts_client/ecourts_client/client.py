"""Top-level entry points for the shared eCourts client.

`fetch_case` and `fetch_pdf` are wrapped with the layered resilience stack:
    Layer 1: semaphore  (cap concurrency)
    Layer 2: circuit breaker  (fail fast when court site is down)
    Layer 3: retry  (transient transport errors only)
    Layer 4: transport  (synchronous DistrictCourtClient / HighCourtClient,
                         run in a worker thread via asyncio.to_thread)
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ecourts_client.config import ECourtsConfig
from ecourts_client.errors import ForumNotAutomated
from ecourts_client.forums import Forum, ForumAdapter
from ecourts_client.models import Case
from ecourts_client.resilience import (
    with_circuit_breaker,
    with_retry,
    with_semaphore,
)
from ecourts_client.routing import classify_cnr, validate_identifier


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


# --- Forum adapter registry ---------------------------------------------
# Additive, forum-first layer that sits alongside (does not replace) the
# CNR-first fetch_case/get_client_for path above. Adapters register a factory
# keyed by Forum; forums with no registered adapter are manual-only.
_ADAPTER_FACTORIES: dict[Forum, Callable[[], ForumAdapter]] = {}


def register_adapter(forum: Forum, factory: Callable[[], ForumAdapter]) -> None:
    """Register (or replace) the adapter factory for a forum."""
    _ADAPTER_FACTORIES[forum] = factory


def has_automated_adapter(forum: Forum) -> bool:
    """True if an automated adapter is registered (i.e. the forum can be fetched)."""
    return forum in _ADAPTER_FACTORIES


def get_adapter(forum: Forum) -> ForumAdapter:
    """Return a fresh adapter for ``forum`` or raise ForumNotAutomated."""
    factory = _ADAPTER_FACTORIES.get(forum)
    if factory is None:
        raise ForumNotAutomated(forum.value if isinstance(forum, Forum) else str(forum))
    return factory()


async def _fetch_case_for_forum_async(forum: Forum, identifier: str) -> Case:
    """Layer-4 thunk for the forum-aware path: validate then run the adapter."""
    import asyncio

    validate_identifier(forum, identifier)
    adapter = get_adapter(forum)
    return await asyncio.to_thread(adapter.fetch_case, identifier)


# Forum-aware sibling of fetch_case(cnr). eCourts forums flow through their
# registered DC/HC adapters; Phase-2/3 forums register their own adapters.
# NOTE: shares the global "ecourts_global" breaker for now — Phase 2 switches to
# per-forum breakers so a non-eCourts outage can't trip the eCourts circuit.
fetch_case_for_forum = _wrap_with_resilience(_fetch_case_for_forum_async)
