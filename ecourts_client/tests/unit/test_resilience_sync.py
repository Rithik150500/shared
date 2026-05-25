"""Tests for the sync resilience layer + thread-safe CircuitBreaker.

Companion to test_resilience_retry.py / test_resilience_circuit_breaker.py /
test_resilience_semaphore.py which cover the async paths.
"""
from __future__ import annotations

import threading
import time

import pytest

from ecourts_client.errors import CourtSiteDown
from ecourts_client.resilience.circuit_breaker import (
    CircuitBreaker,
    _CircuitRegistry,
)


@pytest.fixture(autouse=True)
def _reset_registries():
    """Each test starts with empty named-registry state."""
    from ecourts_client.resilience.semaphore import _SemaphoreRegistry
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()
    yield
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()


def test_circuit_breaker_thread_safety_record_failure():
    """50 threads concurrently calling record_failure must end with a
    deterministic failure_count of exactly 50. Without a lock, lost-update
    races produce a smaller count."""
    cb = CircuitBreaker(name="test_threadsafe", failure_threshold=10000)
    barrier = threading.Barrier(50)

    def hit():
        barrier.wait()
        cb.record_failure()

    threads = [threading.Thread(target=hit) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert cb._failure_count == 50


def test_fire_on_open_uses_snapshot_not_live_state():
    """Snapshot fields are passed to the callback so post-open mutations
    cannot change what the callback sees. Use an async callback that
    captures both args; trigger open from the threshold path; record
    success immediately after (which mutates state); assert the captured
    callback args reflect the pre-mutation snapshot."""
    captured = []
    async def on_open(failure_count, recovery_timeout):
        captured.append((failure_count, recovery_timeout))

    import asyncio
    async def main():
        cb = CircuitBreaker(
            name="test_snapshot", failure_threshold=2,
            recovery_timeout=10.0, on_open=on_open,
        )
        cb.record_failure()
        cb.record_failure()  # trips open, fires on_open with (2, 10.0)
        # IMMEDIATELY mutate state — this would have raced the old code
        cb.record_success()  # resets failure_count to 0
        # Give the scheduled callback time to run
        await asyncio.sleep(0.05)
        assert captured == [(2, 10.0)], (
            f"callback received {captured}, expected [(2, 10.0)] — "
            f"if it shows (0, ...), the snapshot is leaking live state"
        )
    asyncio.run(main())


def test_circuit_registry_get_is_thread_safe():
    """Many threads racing to create the same-named breaker must all
    receive the SAME instance — not 2 different ones. This proves
    _CircuitRegistry.get() is atomic under contention."""
    received_instances = []
    barrier = threading.Barrier(20)

    def fetch():
        barrier.wait()
        cb = _CircuitRegistry.get("race_test")
        received_instances.append(id(cb))

    threads = [threading.Thread(target=fetch) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    unique_ids = set(received_instances)
    assert len(unique_ids) == 1, (
        f"expected 1 shared instance, got {len(unique_ids)}: {unique_ids}"
    )


import asyncio

from ecourts_client.resilience.semaphore import (
    _SemaphoreRegistry,
    with_semaphore,
)


def test_async_semaphore_still_caps_concurrency_with_threading_primitive():
    """The async with_semaphore wrapper must still honor max_concurrency
    after we switch the underlying primitive to threading.BoundedSemaphore."""
    inflight = 0
    peak = [0]
    lock = threading.Lock()

    @with_semaphore(name="async_cap_test", max_concurrency=3)
    async def task():
        nonlocal inflight
        with lock:
            inflight += 1
            peak[0] = max(peak[0], inflight)
        await asyncio.sleep(0.05)
        with lock:
            inflight -= 1

    async def main():
        await asyncio.gather(*[task() for _ in range(10)])

    asyncio.run(main())
    assert peak[0] == 3, f"peak concurrency was {peak[0]}, expected 3"


from ecourts_client.errors import CNRNotFound, RateLimited
from ecourts_client.resilience.retry import with_retry_sync


def test_retry_sync_retries_on_courtsitedown_then_succeeds():
    attempts = [0]

    @with_retry_sync(max_attempts=3, base_delay=0.001)
    def fn():
        attempts[0] += 1
        if attempts[0] < 3:
            raise CourtSiteDown("transient")
        return "ok"

    assert fn() == "ok"
    assert attempts[0] == 3


def test_retry_sync_exhausts_and_raises_last():
    attempts = [0]

    @with_retry_sync(max_attempts=3, base_delay=0.001)
    def fn():
        attempts[0] += 1
        raise CourtSiteDown(f"attempt {attempts[0]}")

    with pytest.raises(CourtSiteDown, match="attempt 3"):
        fn()
    assert attempts[0] == 3


def test_retry_sync_does_not_retry_on_cnrnotfound():
    attempts = [0]

    @with_retry_sync(max_attempts=3, base_delay=0.001)
    def fn():
        attempts[0] += 1
        raise CNRNotFound("DLND010001231234")

    with pytest.raises(CNRNotFound):
        fn()
    assert attempts[0] == 1


def test_retry_sync_does_not_retry_on_rate_limited():
    attempts = [0]

    @with_retry_sync(max_attempts=3, base_delay=0.001)
    def fn():
        attempts[0] += 1
        raise RateLimited("429")

    with pytest.raises(RateLimited):
        fn()
    assert attempts[0] == 1


def test_retry_sync_backoff_timing(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(
        "ecourts_client.resilience.retry.time.sleep",
        lambda secs: sleep_calls.append(secs),
    )

    attempts = [0]

    @with_retry_sync(max_attempts=3, base_delay=1.0, max_delay=30.0)
    def fn():
        attempts[0] += 1
        raise CourtSiteDown("x")

    with pytest.raises(CourtSiteDown):
        fn()
    # 2 sleeps (between the 3 attempts), backoff 1.0 then 2.0
    assert sleep_calls == [1.0, 2.0]


from ecourts_client.resilience.semaphore import with_semaphore_sync


def test_semaphore_sync_caps_concurrency():
    """8 threads but max_concurrency=3 -> at no point should >3 be inside."""
    inflight = 0
    peak = [0]
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    @with_semaphore_sync(name="sync_cap_test", max_concurrency=3)
    def critical():
        nonlocal inflight
        with lock:
            inflight += 1
            peak[0] = max(peak[0], inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1

    def task():
        # Barrier OUTSIDE the semaphore so all 8 threads race for permits
        # at the same instant. If barrier were inside the decorated function,
        # only 3 threads (max_concurrency) could ever reach barrier.wait()
        # while the remaining 5 are blocked on the semaphore -- deadlock.
        barrier.wait()
        critical()

    threads = [threading.Thread(target=task) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak[0] == 3, f"peak concurrency was {peak[0]}, expected 3"


from ecourts_client.errors import CircuitOpen
from ecourts_client.resilience.circuit_breaker import with_circuit_breaker_sync, with_circuit_breaker


def test_circuit_breaker_shared_state_sync_then_async():
    """Sync wrapper trips the breaker after threshold; the very next ASYNC
    call (different wrapper, same breaker name) sees CircuitOpen."""
    @with_circuit_breaker_sync(name="shared_test", failure_threshold=3, recovery_timeout=10.0)
    def sync_fn(): raise CourtSiteDown("sync fail")

    @with_circuit_breaker(name="shared_test", failure_threshold=3, recovery_timeout=10.0)
    async def async_fn(): return "ok"

    # Trip the breaker via sync
    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            sync_fn()

    # Now async wrapper must see the open breaker
    with pytest.raises(CircuitOpen):
        asyncio.run(async_fn())


def test_circuit_breaker_shared_state_async_then_sync():
    """Reverse direction -- proves sharing is genuinely symmetric."""
    @with_circuit_breaker(name="shared_test_2", failure_threshold=3, recovery_timeout=10.0)
    async def async_fn(): raise CourtSiteDown("async fail")

    @with_circuit_breaker_sync(name="shared_test_2", failure_threshold=3, recovery_timeout=10.0)
    def sync_fn(): return "ok"

    async def trip():
        for _ in range(3):
            with pytest.raises(CourtSiteDown):
                await async_fn()
    asyncio.run(trip())

    with pytest.raises(CircuitOpen):
        sync_fn()


def test_sync_helpers_exported_from_resilience_package():
    from ecourts_client.resilience import (
        with_retry_sync,
        with_semaphore_sync,
        with_circuit_breaker_sync,
    )
    assert callable(with_retry_sync)
    assert callable(with_semaphore_sync)
    assert callable(with_circuit_breaker_sync)
