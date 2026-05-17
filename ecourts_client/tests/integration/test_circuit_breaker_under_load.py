"""Circuit breaker under concurrent load.

Verifies:
  * Concurrent failing calls open the breaker after the threshold.
  * Once open, subsequent in-flight callers fail fast with CircuitOpen
    rather than continuing to hammer the failing transport.
  * After the recovery_timeout elapses and a half-open probe succeeds,
    the breaker re-closes and traffic flows again.
"""
from __future__ import annotations

import asyncio

import pytest

from ecourts_client.errors import CircuitOpen, CourtSiteDown
from ecourts_client.resilience.circuit_breaker import (
    _CircuitRegistry,
    with_circuit_breaker,
)
from ecourts_client.resilience.semaphore import (
    _SemaphoreRegistry,
    with_semaphore,
)


@pytest.mark.asyncio
async def test_concurrent_failures_open_breaker_quickly():
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()
    invocations = {"n": 0, "fast_fails": 0}

    @with_semaphore(name="ld", max_concurrency=20)
    @with_circuit_breaker(name="ld", failure_threshold=5, recovery_timeout=10.0)
    async def fails():
        invocations["n"] += 1
        await asyncio.sleep(0.001)
        raise CourtSiteDown("boom")

    async def caller():
        try:
            await fails()
        except CircuitOpen:
            invocations["fast_fails"] += 1
        except CourtSiteDown:
            pass

    # Launch 50 concurrent callers against a breaker whose threshold is 5.
    await asyncio.gather(*[caller() for _ in range(50)])

    # The breaker should have opened well before all 50 reached the transport.
    cb = _CircuitRegistry.get("ld")
    assert cb.state == "open"
    assert invocations["fast_fails"] > 0
    # Conservative: we should not have invoked the transport 50 times.
    assert invocations["n"] < 50


@pytest.mark.asyncio
async def test_breaker_recovers_when_transport_heals():
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()
    transport_healed = {"flag": False}

    @with_semaphore(name="ld2", max_concurrency=10)
    @with_circuit_breaker(name="ld2", failure_threshold=3, recovery_timeout=0.05)
    async def call():
        if transport_healed["flag"]:
            return "ok"
        raise CourtSiteDown("transient")

    # Drive enough failures to open the breaker.
    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            await call()
    cb = _CircuitRegistry.get("ld2")
    assert cb.state == "open"

    # While open, calls fail fast.
    with pytest.raises(CircuitOpen):
        await call()

    # Wait past the recovery timeout, heal the transport, fire a probe.
    await asyncio.sleep(0.06)
    transport_healed["flag"] = True
    result = await call()
    assert result == "ok"
    assert cb.state == "closed"
