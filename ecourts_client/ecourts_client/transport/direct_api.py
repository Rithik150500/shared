"""Resilient default transport. Uses the four-layer stack composed on `client`.

This is the public production path: every call goes through
semaphore -> circuit breaker -> retry -> the synchronous DistrictCourtClient
/ HighCourtClient (off-loaded via asyncio.to_thread by `client.py`).

Tests that want to bypass the stack should use `transport.raw.RawTransport`.
"""
from __future__ import annotations

from ecourts_client.client import fetch_case as _fetch_case
from ecourts_client.client import fetch_pdf as _fetch_pdf
from ecourts_client.models import Case


class DirectAPITransport:
    """Async, resilience-wrapped transport (the production default)."""

    async def fetch_case(self, cnr: str) -> Case:
        return await _fetch_case(cnr)

    async def fetch_pdf(self, url: str, cnr_hint: str | None = None) -> bytes:
        return await _fetch_pdf(url, cnr_hint)
