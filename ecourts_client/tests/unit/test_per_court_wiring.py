"""The per-court policy must reach the REAL composed async fetch stack.

Tests that build their own decorator with an explicit ``per_court=`` would pass
even if ``client._wrap_with_resilience`` never plumbed it through -- which is
the entire change.

Scope note: per-court keying is applied to the async fetch path (fetch_case /
fetch_pdf) only. The sync picker/search methods in ``_resilience_apply`` take
court arguments rather than CNRs and stay on the global breaker for now; their
court-scoped failures fall back to the global breaker by design
(``_run_outcome`` routes TRIP_COURT to global when there is no court breaker).
"""
from __future__ import annotations

import pytest

from ecourts_client import client as client_mod
from ecourts_client.config import ECourtsConfig
from ecourts_client.errors import CircuitOpen, CourtSiteDown
from ecourts_client.resilience.circuit_breaker import _CircuitRegistry
from ecourts_client.resilience.court_key import court_key_for_cnr

DL = "DLHC010012342023"   # hc:DL
RJ = "RJAU019999992015"   # dc:RJ


def _cfg() -> ECourtsConfig:
    return ECourtsConfig(
        ecourts_failure_taxonomy=True,
        ecourts_per_court_circuit=True,
        ecourts_court_failure_threshold=2,
        ecourts_circuit_failure_threshold=99,   # keep global out of the way
    )


@pytest.mark.asyncio
async def test_async_fetch_stack_keys_the_breaker_by_court(monkeypatch):
    _CircuitRegistry.reset()
    monkeypatch.setattr(client_mod, "_CONFIG", _cfg())

    async def raw(cnr):
        if cnr.startswith("DL"):
            raise CourtSiteDown("502")
        return "ok"

    wrapped = client_mod._wrap_with_resilience(
        raw, key_fn=lambda cnr, *a, **k: court_key_for_cnr(cnr)
    )

    for _ in range(2):
        with pytest.raises(CourtSiteDown):
            await wrapped(DL)
    with pytest.raises(CircuitOpen):
        await wrapped(DL)               # Delhi HC is open...
    assert await wrapped(RJ) == "ok"    # ...Rajasthan is not

    names = {n for n, _ in _CircuitRegistry.all_items()}
    assert "hc:DL" in names, f"expected a per-court breaker, got {sorted(names)}"


@pytest.mark.asyncio
async def test_flag_off_keeps_one_global_breaker(monkeypatch):
    _CircuitRegistry.reset()
    monkeypatch.setattr(
        client_mod, "_CONFIG",
        ECourtsConfig(ecourts_failure_taxonomy=True, ecourts_per_court_circuit=False,
                      ecourts_circuit_failure_threshold=2),
    )

    async def raw(cnr):
        raise CourtSiteDown("502")

    wrapped = client_mod._wrap_with_resilience(
        raw, key_fn=lambda cnr, *a, **k: court_key_for_cnr(cnr)
    )
    for _ in range(2):
        with pytest.raises(CourtSiteDown):
            await wrapped(DL)
    with pytest.raises(CircuitOpen):
        await wrapped(RJ)               # global took the hit -> everything blocked
    assert not any(n.startswith(("dc:", "hc:")) for n, _ in _CircuitRegistry.all_items())


def test_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv("ECOURTS_PER_COURT_CIRCUIT", raising=False)
    assert ECourtsConfig().ecourts_per_court_circuit is False
