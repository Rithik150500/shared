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
from ecourts_client.forums import ECOURTS_FORUMS, Forum, ForumAdapter, TribunalKind
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


def _wrap_with_resilience(fn, *, name: str = "ecourts_global"):
    return with_semaphore(name=name, max_concurrency=_CONFIG.ecourts_max_concurrency)(
        with_circuit_breaker(
            name=name,
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
# Registry key is a (forum, kind) composite. Single-forum adapters register
# under (forum, None); tribunal adapters register per-kind under
# (Forum.TRIBUNAL, kind). `kind` is keyword-only with a None default EVERYWHERE,
# so every existing Forum-only call (eCourts/consumer/SC) is unchanged.
_AdapterKey = tuple[Forum, "TribunalKind | None"]
_ADAPTER_FACTORIES: dict[_AdapterKey, Callable[[], ForumAdapter]] = {}


def register_adapter(
    forum: Forum,
    factory: Callable[[], ForumAdapter],
    *,
    kind: "TribunalKind | None" = None,
) -> None:
    """Register (or replace) the adapter factory for a (forum, kind)."""
    _ADAPTER_FACTORIES[(forum, kind)] = factory


def has_automated_adapter(forum: Forum, *, kind: "TribunalKind | None" = None) -> bool:
    """True if an automated adapter is registered for (forum, kind)."""
    return (forum, kind) in _ADAPTER_FACTORIES


def get_adapter(forum: Forum, *, kind: "TribunalKind | None" = None) -> ForumAdapter:
    """Return a fresh adapter for (forum, kind) or raise ForumNotAutomated."""
    factory = _ADAPTER_FACTORIES.get((forum, kind))
    if factory is None:
        label = forum.value if isinstance(forum, Forum) else str(forum)
        if kind is not None:
            label += f":{kind.value if isinstance(kind, TribunalKind) else kind}"
        raise ForumNotAutomated(label)
    return factory()


async def _fetch_case_for_forum_async(
    forum: Forum, identifier: str, kind: "TribunalKind | None" = None
) -> Case:
    """Layer-4 thunk for the forum-aware path: validate then run the adapter."""
    import asyncio

    validate_identifier(forum, identifier)
    adapter = get_adapter(forum, kind=kind)
    return await asyncio.to_thread(adapter.fetch_case, identifier)


# Forum-aware sibling of fetch_case(cnr). eCourts forums flow through their
# registered DC/HC adapters and SHARE the "ecourts_global" breaker/semaphore
# (same backend as the CNR-first path). Non-eCourts forums (consumer, …) get an
# ISOLATED per-forum breaker ("forum_<value>"); tribunal KINDS get a per-kind
# breaker ("forum_tribunal_<kind>") so e.g. an NCLAT outage can't trip ITAT. The
# wrapped fetcher is built once per (forum, kind) and cached.
_FORUM_FETCHERS: dict[_AdapterKey, Callable] = {}


def _forum_fetcher(forum: Forum, kind: "TribunalKind | None" = None) -> Callable:
    key = (forum, kind)
    fetcher = _FORUM_FETCHERS.get(key)
    if fetcher is None:
        if forum in ECOURTS_FORUMS:
            name = "ecourts_global"
        elif kind is not None:
            name = f"forum_tribunal_{kind.value if isinstance(kind, TribunalKind) else kind}"
        else:
            name = f"forum_{forum.value}"
        fetcher = _wrap_with_resilience(_fetch_case_for_forum_async, name=name)
        _FORUM_FETCHERS[key] = fetcher
    return fetcher


async def fetch_case_for_forum(
    forum: Forum, identifier: str, *, kind: "TribunalKind | None" = None
) -> Case:
    """Resilience-wrapped forum-aware fetch; per-forum/per-kind breaker isolation."""
    return await _forum_fetcher(forum, kind)(forum, identifier, kind)
