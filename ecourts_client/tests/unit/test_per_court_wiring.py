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
from ecourts_client.resilience.court_key import GLOBAL_KEY, court_key_for_cnr

DL = "DLHC010012342023"   # hc:DL
RJ = "RJAU019999992015"   # dc:RJ


def _cfg(**over) -> ECourtsConfig:
    base = dict(
        ecourts_failure_taxonomy=True,
        ecourts_per_court_circuit=True,
        ecourts_court_failure_threshold=2,
        ecourts_circuit_failure_threshold=99,   # keep global out of the way
        ecourts_retry_max_attempts=1,           # no real retry sleeps in tests
    )
    base.update(over)
    return ECourtsConfig(**base)


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


# --- plumbing guards: these fail if the config value stops reaching the policy ---

@pytest.mark.asyncio
async def test_config_failure_window_reaches_the_court_breaker(monkeypatch):
    """Kills the mutant `failure_window_seconds=None`.

    With consecutive counting the interleaved success would zero the count, so
    a coarse state-level key could never accumulate enough to open.
    """
    _CircuitRegistry.reset()
    monkeypatch.setattr(client_mod, "_CONFIG", _cfg(ecourts_court_failure_threshold=2))
    calls = {"n": 0}

    async def raw(cnr):
        calls["n"] += 1
        if calls["n"] % 2 == 1:      # fail, ok, fail, ...
            raise CourtSiteDown("502")
        return "ok"

    wrapped = client_mod._wrap_with_resilience(
        raw, key_fn=lambda cnr, *a, **k: court_key_for_cnr(cnr)
    )
    with pytest.raises(CourtSiteDown):
        await wrapped(DL)
    assert await wrapped(DL) == "ok"      # success must NOT heal the window
    with pytest.raises(CourtSiteDown):
        await wrapped(DL)
    with pytest.raises(CircuitOpen):
        await wrapped(DL)


@pytest.mark.asyncio
async def test_config_cascade_threshold_reaches_the_policy(monkeypatch):
    """Kills the mutant `cascade_open_threshold=0`."""
    _CircuitRegistry.reset()
    monkeypatch.setattr(
        client_mod, "_CONFIG",
        _cfg(ecourts_court_failure_threshold=1, ecourts_cascade_open_court_threshold=2),
    )

    async def raw(cnr):
        raise CourtSiteDown("502")

    wrapped = client_mod._wrap_with_resilience(
        raw, key_fn=lambda cnr, *a, **k: court_key_for_cnr(cnr)
    )
    for cnr in (DL, RJ):
        with pytest.raises(CourtSiteDown):
            await wrapped(cnr)
    assert _CircuitRegistry.get(GLOBAL_KEY).state == "open"


@pytest.mark.asyncio
async def test_the_real_module_level_fetch_case_is_court_keyed(monkeypatch):
    """Kills the mutant that drops `key_fn=` from the production bindings.

    The other tests call _wrap_with_resilience themselves and pass their own
    lambda, which proves the parameter is plumbed -- not that the shipped call
    sites supply one. This drives the real `ecourts_client.client.fetch_case`.
    """
    import importlib

    # Reloading rebinds module-level state, including the forum ADAPTER
    # registry that other modules populate at import time. Losing it breaks
    # unrelated tribunal/consumer registry tests, so snapshot and restore it.
    saved_adapters = dict(client_mod._ADAPTER_FACTORIES)
    saved_fetchers = dict(client_mod._FORUM_FETCHERS)

    monkeypatch.setenv("ECOURTS_FAILURE_TAXONOMY", "1")
    monkeypatch.setenv("ECOURTS_PER_COURT_CIRCUIT", "1")
    monkeypatch.setenv("ECOURTS_COURT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("ECOURTS_RETRY_MAX_ATTEMPTS", "1")
    reloaded = importlib.reload(client_mod)
    try:
        _CircuitRegistry.reset()

        class _Down:
            def fetch_case(self, cnr):
                raise CourtSiteDown("502")

        monkeypatch.setattr(reloaded, "get_client_for", lambda cnr: _Down())
        with pytest.raises(CourtSiteDown):
            await reloaded.fetch_case(DL)
        names = {n for n, _ in _CircuitRegistry.all_items()}
        assert "hc:DL" in names, f"real fetch_case is not court-keyed: {sorted(names)}"

        # fetch_pdf keys off the optional cnr_hint. Hint-less calls route to the
        # global breaker (UNKNOWN_KEY), but a HINTED fetch must reach its court.
        class _DownPdf:
            def fetch_pdf(self, url):
                raise CourtSiteDown("502")

        monkeypatch.setattr(reloaded, "get_client_for", lambda cnr: _DownPdf())
        _CircuitRegistry.reset()
        with pytest.raises(CourtSiteDown):
            await reloaded.fetch_pdf("https://example.invalid/o.pdf", cnr_hint=RJ)
        names = {n for n, _ in _CircuitRegistry.all_items()}
        assert "dc:RJ" in names, f"real fetch_pdf is not court-keyed: {sorted(names)}"
    finally:
        for var in ("ECOURTS_FAILURE_TAXONOMY", "ECOURTS_PER_COURT_CIRCUIT",
                    "ECOURTS_COURT_FAILURE_THRESHOLD", "ECOURTS_RETRY_MAX_ATTEMPTS"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(client_mod)
        client_mod._ADAPTER_FACTORIES.update(saved_adapters)
        client_mod._FORUM_FETCHERS.update(saved_fetchers)
        _CircuitRegistry.reset()
