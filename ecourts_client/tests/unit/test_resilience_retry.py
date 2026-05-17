"""Layer-3 retry -- only on CourtSiteDown / network errors; exponential backoff."""
from __future__ import annotations

import asyncio
import pytest

from ecourts_client.errors import CNRNotFound, CourtSiteDown
from ecourts_client.resilience.retry import with_retry


@pytest.mark.asyncio
async def test_retry_eventually_succeeds():
    attempts = {"n": 0}

    @with_retry(max_attempts=3, base_delay=0.005)
    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise CourtSiteDown("transient")
        return "ok"

    assert (await flaky()) == "ok"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_retry_exhausts_and_raises():
    @with_retry(max_attempts=2, base_delay=0.005)
    async def always_fails():
        raise CourtSiteDown("perm")

    with pytest.raises(CourtSiteDown):
        await always_fails()


@pytest.mark.asyncio
async def test_retry_does_not_retry_terminal_errors():
    attempts = {"n": 0}

    @with_retry(max_attempts=3, base_delay=0.005)
    async def cnr_missing():
        attempts["n"] += 1
        raise CNRNotFound("MHCC010054732024")

    with pytest.raises(CNRNotFound):
        await cnr_missing()
    assert attempts["n"] == 1
