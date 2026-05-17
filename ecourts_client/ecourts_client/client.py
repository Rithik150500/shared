"""Top-level entry points for the shared eCourts client.

Phase 1: no resilience wrappers (added in Task 1.13).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ecourts_client.errors import CNRMalformed
from ecourts_client.models import Case
from ecourts_client.routing import classify_cnr


@runtime_checkable
class ECourtsClient(Protocol):
    scope: str

    def fetch_case(self, cnr: str) -> Case: ...
    def fetch_pdf(self, url: str) -> bytes: ...


def get_client_for(cnr: str) -> ECourtsClient:
    scope = classify_cnr(cnr)
    if scope == "district":
        from ecourts_client.district import DistrictCourtClient
        return DistrictCourtClient()
    from ecourts_client.highcourt import HighCourtClient
    return HighCourtClient()


def fetch_case(cnr: str) -> Case:
    return get_client_for(cnr).fetch_case(cnr)


def fetch_pdf(url: str, cnr_hint: str | None = None) -> bytes:
    """Fetch a PDF using a session matching `cnr_hint`'s scope; defaults to district."""
    if cnr_hint:
        return get_client_for(cnr_hint).fetch_pdf(url)
    from ecourts_client.district import DistrictCourtClient
    return DistrictCourtClient().fetch_pdf(url)
