"""client.fetch_case is wrapped with semaphore -> circuit_breaker -> retry."""
from __future__ import annotations

import inspect

import pytest

from ecourts_client.client import fetch_case, fetch_pdf
from ecourts_client.errors import CNRNotFound, CourtSiteDown
from ecourts_client.resilience.circuit_breaker import _CircuitRegistry
from ecourts_client.resilience.retry import with_retry
from ecourts_client.resilience.circuit_breaker import with_circuit_breaker
from ecourts_client.resilience.semaphore import with_semaphore, _SemaphoreRegistry


def test_fetch_case_is_async():
    assert inspect.iscoroutinefunction(fetch_case)


def test_fetch_pdf_is_async():
    assert inspect.iscoroutinefunction(fetch_pdf)


@pytest.mark.asyncio
async def test_decorator_stack_composes():
    """All four layers stack: semaphore -> circuit breaker -> retry -> business fn."""
    _SemaphoreRegistry.reset()
    _CircuitRegistry.reset()
    call_count = {"n": 0}

    @with_semaphore(name="stack", max_concurrency=2)
    @with_circuit_breaker(name="stack", failure_threshold=10, recovery_timeout=10.0)
    @with_retry(max_attempts=3, base_delay=0.001)
    async def flaky():
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise CourtSiteDown("transient")
        return "ok"

    result = await flaky()
    assert result == "ok"
    # Retry layer should have invoked the inner fn 3 times before success.
    assert call_count["n"] == 3
    # Circuit breaker should still be closed because the wrapped call returned ok.
    cb = _CircuitRegistry.get("stack")
    assert cb.state == "closed"


@pytest.mark.asyncio
async def test_decorator_stack_terminal_error_propagates():
    """Terminal errors (CNRNotFound) bypass retry and reach the caller."""
    _SemaphoreRegistry.reset()
    _CircuitRegistry.reset()
    call_count = {"n": 0}

    @with_semaphore(name="stack2", max_concurrency=2)
    @with_circuit_breaker(name="stack2", failure_threshold=10, recovery_timeout=10.0)
    @with_retry(max_attempts=3, base_delay=0.001)
    async def terminal():
        call_count["n"] += 1
        raise CNRNotFound("MHCC010054732024")

    with pytest.raises(CNRNotFound):
        await terminal()
    # CNRNotFound is not retriable -> exactly one invocation.
    assert call_count["n"] == 1
