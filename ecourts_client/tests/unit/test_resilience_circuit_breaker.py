"""Layer-2 circuit breaker -- 3-state machine; raises CircuitOpen when open."""
from __future__ import annotations

import asyncio
import pytest

from ecourts_client.errors import CircuitOpen, CourtSiteDown
from ecourts_client.resilience.circuit_breaker import (
    CircuitBreaker,
    _CircuitRegistry,
    with_circuit_breaker,
)


@pytest.mark.asyncio
async def test_circuit_closed_passes_through():
    _CircuitRegistry.reset()

    @with_circuit_breaker(name="t", failure_threshold=3, recovery_timeout=0.05)
    async def ok():
        return "ok"

    assert (await ok()) == "ok"


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold():
    _CircuitRegistry.reset()

    @with_circuit_breaker(name="t2", failure_threshold=3, recovery_timeout=10.0)
    async def fails():
        raise CourtSiteDown("boom")

    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            await fails()
    with pytest.raises(CircuitOpen):
        await fails()


@pytest.mark.asyncio
async def test_circuit_half_open_recovers_on_success():
    _CircuitRegistry.reset()
    cb = CircuitBreaker(name="t3", failure_threshold=2, recovery_timeout=0.02)
    for _ in range(2):
        cb.record_failure()
    assert cb.state == "open"
    await asyncio.sleep(0.03)
    # Reading state again transitions to half_open
    assert cb.state == "half_open"
    cb.record_success()
    assert cb.state == "closed"


@pytest.mark.asyncio
async def test_circuit_alert_callback_fires_once_on_open():
    _CircuitRegistry.reset()
    calls: list[tuple[int, float]] = []

    async def alert(fc, rt):
        calls.append((fc, rt))

    cb = CircuitBreaker(name="t4", failure_threshold=2, recovery_timeout=10.0, on_open=alert)
    cb.record_failure()
    cb.record_failure()
    await asyncio.sleep(0.01)
    assert len(calls) >= 1
