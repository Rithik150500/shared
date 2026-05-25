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
