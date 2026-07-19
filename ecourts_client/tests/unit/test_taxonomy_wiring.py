"""The taxonomy flag must actually reach the real composed stacks.

Tests that build their own decorator with an explicit ``use_taxonomy=True``
would pass even if ``client.py`` and ``_resilience_apply.py`` forgot to plumb
the flag through -- which is the whole change. These exercise the real
composition (semaphore -> circuit breaker -> retry) instead.
"""
from __future__ import annotations

import pytest

from ecourts_client import client as client_mod
from ecourts_client._resilience_apply import _build_wrapped
from ecourts_client.config import ECourtsConfig
from ecourts_client.errors import CNRNotFound
from ecourts_client.resilience.circuit_breaker import _CircuitRegistry

_BAD_CNR = "MHAU019999992015"


def _cfg() -> ECourtsConfig:
    return ECourtsConfig(
        ecourts_circuit_failure_threshold=3,
        ecourts_failure_taxonomy=True,
    )


def test_sync_picker_stack_passes_the_flag_through():
    """_resilience_apply._build_wrapped must forward the flag to the breaker."""
    _CircuitRegistry.reset()

    def raw(_self):
        raise CNRNotFound(_BAD_CNR)

    wrapped = _build_wrapped(raw, config=_cfg())
    for _ in range(6):
        with pytest.raises(CNRNotFound):
            wrapped(None)


@pytest.mark.asyncio
async def test_async_fetch_stack_passes_the_flag_through(monkeypatch):
    """client._wrap_with_resilience must forward the flag to the breaker."""
    _CircuitRegistry.reset()
    monkeypatch.setattr(client_mod, "_CONFIG", _cfg())

    async def raw():
        raise CNRNotFound(_BAD_CNR)

    wrapped = client_mod._wrap_with_resilience(raw)
    for _ in range(6):
        with pytest.raises(CNRNotFound):
            await wrapped()


def test_flag_defaults_to_off(monkeypatch):
    """Ship dark: the code default must not change today's behaviour.

    Hermetic on purpose -- this asserts the *code* default, so the ambient
    ECOURTS_FAILURE_TAXONOMY (set when the suite is run flag-on in CI) must be
    cleared or the test would just be reading the environment back.
    """
    monkeypatch.delenv("ECOURTS_FAILURE_TAXONOMY", raising=False)
    assert ECourtsConfig().ecourts_failure_taxonomy is False
