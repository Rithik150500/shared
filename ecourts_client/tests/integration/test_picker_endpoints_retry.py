"""End-to-end: a real DistrictCourtClient.list_states() call against a
mocked-flaky eCourts must succeed after the sync retry layer kicks in.

Parser-shape note (observed in ecourts_client/parsers/dropdowns.py):
    stateWebService.php response envelope is {"states": <list-or-dict>}, with
    rows shaped {state_code, state_name, nationalstate_code}. The plan's
    suggested {"stateList": [...], "state_code_NIC": ...} shape would fail
    parse_states with a SchemaChanged (missing 'states' field). We use the
    real shape below.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests
import responses

from ecourts_client import DistrictCourtClient
from ecourts_client._resilience_apply import _RESILIENCE_MARKER
from ecourts_client.resilience.circuit_breaker import _CircuitRegistry
from ecourts_client.resilience.semaphore import _SemaphoreRegistry


_DC_BASE = "https://app.ecourts.gov.in/services_DC_4.0/"  # eCourts mobile API v4.0 (2026-07-02)


@pytest.fixture(autouse=True)
def _reset_state():
    """Fresh circuit + semaphore for each test."""
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()
    yield
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()


def test_list_states_method_is_wrapped():
    """Sanity check: the very method we're about to exercise is decorated."""
    assert getattr(DistrictCourtClient.list_states, _RESILIENCE_MARKER, False) is True


@responses.activate
def test_list_states_recovers_from_transient_connection_drops():
    """Mock appReleaseWebService.php to return a JWT once, then have
    stateWebService.php raise ConnectionError twice before returning OK
    on the third try. The retry layer should hide both drops; the caller
    sees a successful result."""

    # 1) Bootstrap JWT: single 200 response with a token in plain-JSON.
    # The _session._send cleartext-path branch accepts plain-JSON bodies
    # starting with { and ending with }, so we return that directly.
    responses.add(
        responses.GET,
        f"{_DC_BASE}appReleaseWebService.php",
        body=json.dumps({"token": "fake_jwt_for_test", "status": "Y"}),
        status=200,
        content_type="application/json",
    )

    # 2) stateWebService.php: ConnectionError, ConnectionError, 200 OK.
    responses.add(
        responses.GET,
        f"{_DC_BASE}stateWebService.php",
        body=requests.ConnectionError("simulated RemoteDisconnected"),
    )
    responses.add(
        responses.GET,
        f"{_DC_BASE}stateWebService.php",
        body=requests.ConnectionError("simulated RemoteDisconnected"),
    )
    # Third call: success. Envelope shape per parsers/dropdowns.py::parse_states:
    #   top-level key is "states" (NOT "stateList"); each row uses keys
    #   state_code / state_name / nationalstate_code (NOT state_code_NIC).
    fake_states_body = json.dumps({
        "status": "Y",
        "states": [
            {"state_code": "1", "state_name": "Maharashtra", "nationalstate_code": "MH"},
            {"state_code": "2", "state_name": "Andhra Pradesh", "nationalstate_code": "AP"},
        ],
    })
    responses.add(
        responses.GET,
        f"{_DC_BASE}stateWebService.php",
        body=fake_states_body,
        status=200,
        content_type="application/json",
    )

    # Speed up: monkey-patch the retry backoff so the test doesn't sleep
    # 1+2 = 3 seconds.
    with patch("ecourts_client.resilience.retry.time.sleep"):
        client = DistrictCourtClient()
        states = client.list_states()

    # Assertions
    assert len(states) == 2, f"expected 2 states, got {len(states)}: {states}"
    assert states[0].name == "Maharashtra", f"got first state {states[0]}"
    # 1 bootstrap + 3 stateWebService.php attempts = 4 GETs
    assert len(responses.calls) == 4, f"expected 4 calls, got {len(responses.calls)}"
