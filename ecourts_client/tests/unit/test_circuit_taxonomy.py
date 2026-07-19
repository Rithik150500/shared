"""The circuit breaker must count only real availability failures.

Guards the defect where ``except Exception: cb.record_failure()`` treated every
error as an outage signal, so a handful of user-typed bad CNRs opened the
process-wide breaker for every tenant.
"""
from __future__ import annotations

import pytest

from ecourts_client.errors import CircuitOpen, CNRNotFound, CourtSiteDown, RateLimited
from ecourts_client.resilience.circuit_breaker import (
    _CircuitRegistry,
    with_circuit_breaker,
    with_circuit_breaker_sync,
)


@pytest.mark.asyncio
async def test_bad_cnrs_never_open_the_breaker():
    """The headline regression. Ten bad CNRs, threshold 3, breaker stays closed."""
    _CircuitRegistry.reset()

    @with_circuit_breaker(
        name="tax_cnr", failure_threshold=3, recovery_timeout=10.0, use_taxonomy=True
    )
    async def not_found():
        raise CNRNotFound("MHAU019999992015")

    for _ in range(10):
        with pytest.raises(CNRNotFound):
            await not_found()


@pytest.mark.asyncio
async def test_rate_limited_still_opens_the_breaker():
    """IP-wide throttle is exactly what the breaker exists for."""
    _CircuitRegistry.reset()

    @with_circuit_breaker(
        name="tax_429", failure_threshold=3, recovery_timeout=10.0, use_taxonomy=True
    )
    async def limited():
        raise RateLimited("429")

    for _ in range(3):
        with pytest.raises(RateLimited):
            await limited()
    with pytest.raises(CircuitOpen):
        await limited()


@pytest.mark.asyncio
async def test_court_site_down_still_opens_the_breaker():
    _CircuitRegistry.reset()

    @with_circuit_breaker(
        name="tax_5xx", failure_threshold=3, recovery_timeout=10.0, use_taxonomy=True
    )
    async def down():
        raise CourtSiteDown("502")

    for _ in range(3):
        with pytest.raises(CourtSiteDown):
            await down()
    with pytest.raises(CircuitOpen):
        await down()


@pytest.mark.asyncio
async def test_flag_off_preserves_todays_behaviour():
    """Parity: with the flag off, a bad CNR opens the breaker exactly as it does today."""
    _CircuitRegistry.reset()

    @with_circuit_breaker(name="tax_off", failure_threshold=3, recovery_timeout=10.0)
    async def not_found():
        raise CNRNotFound("MHAU019999992015")

    for _ in range(3):
        with pytest.raises(CNRNotFound):
            await not_found()
    with pytest.raises(CircuitOpen):
        await not_found()


def test_sync_wrapper_bad_cnrs_never_open_the_breaker():
    _CircuitRegistry.reset()

    @with_circuit_breaker_sync(
        name="tax_sync", failure_threshold=3, recovery_timeout=10.0, use_taxonomy=True
    )
    def not_found():
        raise CNRNotFound("MHAU019999992015")

    for _ in range(10):
        with pytest.raises(CNRNotFound):
            not_found()


def test_neutral_errors_do_not_heal_an_accumulating_failure_count():
    """NEUTRAL means "no signal" -- it must not reset the counter either.

    The breaker counts CONSECUTIVE failures (record_success zeroes the count),
    so if a neutral error recorded a success an interleaved bad-CNR stream
    would mask a real outage. Neutral must be a no-op on both paths.
    """
    _CircuitRegistry.reset()

    @with_circuit_breaker_sync(
        name="tax_interleaved", failure_threshold=3, recovery_timeout=10.0, use_taxonomy=True
    )
    def boom(exc):
        raise exc

    # Two real failures, each followed by a neutral one. If neutral healed the
    # count, each pair would reset to zero and the breaker could never open.
    for i in ("1", "2"):
        with pytest.raises(CourtSiteDown):
            boom(CourtSiteDown(i))
        with pytest.raises(CNRNotFound):
            boom(CNRNotFound("MHAU019999992015"))

    # The third real failure reaches the threshold despite the interleaving.
    with pytest.raises(CourtSiteDown):
        boom(CourtSiteDown("3"))
    with pytest.raises(CircuitOpen):
        boom(CourtSiteDown("4"))
