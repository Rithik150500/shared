"""Shared test fixtures for the eCourts client suite."""
from __future__ import annotations

import pytest

from ecourts_client import _session


@pytest.fixture(autouse=True)
def _disable_ecourts_rate_gate(monkeypatch):
    """The process-wide rate gate (default ~0.34s between calls, see
    _session._RateGate) would add real wall-clock sleeps to every hermetic
    transport test. Install a zero-interval gate for the duration of each test so
    the suite stays fast and deterministic; monkeypatch restores the lazy
    singleton afterward. Tests that exercise the gate itself (test_rate_gate.py)
    build their own _RateGate instances and are unaffected.
    """
    monkeypatch.setattr(_session, "_rate_gate", _session._RateGate(0.0))
