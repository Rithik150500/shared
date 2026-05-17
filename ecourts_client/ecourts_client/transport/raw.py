"""Resilience-free transport. Use only in tests + the canary suite."""
from __future__ import annotations

from ecourts_client.client import get_client_for
from ecourts_client.models import Case


class RawTransport:
    """Bypasses semaphore + circuit breaker + retry. Goes straight to the transport."""

    def fetch_case(self, cnr: str) -> Case:
        return get_client_for(cnr).fetch_case(cnr)

    def fetch_pdf(self, url: str, cnr_hint: str | None = None) -> bytes:
        if cnr_hint:
            return get_client_for(cnr_hint).fetch_pdf(url)
        from ecourts_client.district import DistrictCourtClient
        return DistrictCourtClient().fetch_pdf(url)
