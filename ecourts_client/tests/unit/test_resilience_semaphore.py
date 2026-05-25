"""Layer-1 semaphore caps concurrent in-flight calls."""
from __future__ import annotations

import asyncio
import pytest

from ecourts_client.resilience.semaphore import with_semaphore, _SemaphoreRegistry


@pytest.mark.asyncio
async def test_semaphore_caps_inflight():
    _SemaphoreRegistry.reset()
    inflight = 0
    max_seen = 0
    lock = asyncio.Lock()

    @with_semaphore(name="t", max_concurrency=2)
    async def slow():
        nonlocal inflight, max_seen
        async with lock:
            inflight += 1
            max_seen = max(max_seen, inflight)
        await asyncio.sleep(0.05)
        async with lock:
            inflight -= 1

    await asyncio.gather(*[slow() for _ in range(8)])
    assert max_seen == 2


@pytest.mark.asyncio
async def test_semaphore_registry_singleton():
    _SemaphoreRegistry.reset()

    @with_semaphore(name="t", max_concurrency=1)
    async def a():
        return "a"

    @with_semaphore(name="t", max_concurrency=1)
    async def b():
        return "b"

    assert (await a()) == "a"
    assert (await b()) == "b"
    # After both calls return, all permits should be available again.
    # threading.BoundedSemaphore has no .locked() method; use the
    # non-blocking acquire/release pattern as the behavior-equivalent check.
    sem = _SemaphoreRegistry.get("t")
    acquired = sem.acquire(blocking=False)
    assert acquired is True, "semaphore was still held after both calls returned"
    sem.release()
