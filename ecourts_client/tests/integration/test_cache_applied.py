"""End-to-end wiring of the picker cache + static-state fallback on the REAL
DistrictCourtClient, exercised through the whole resilience stack.

Proves the two behaviors the fix is for:
  * a cached picker list is served with ZERO eCourts HTTP -- and therefore
    survives an OPEN circuit breaker (the throttle-degrade), and
  * list_states never dead-ends: a throttled state fetch yields the baked-in
    static list, not an error.
"""
from __future__ import annotations

import json

import fakeredis
import pytest
import responses

from ecourts_client import DistrictCourtClient
from ecourts_client.cache.backend import clear_backend, set_backend
from ecourts_client.errors import CircuitOpen, RateLimited
from ecourts_client.resilience.circuit_breaker import _CircuitRegistry
from ecourts_client.resilience.semaphore import _SemaphoreRegistry

_DC_BASE = "https://app.ecourts.gov.in/services_DC_4.0/"


@pytest.fixture(autouse=True)
def _reset():
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()
    clear_backend()
    yield
    _CircuitRegistry.reset()
    _SemaphoreRegistry.reset()
    clear_backend()


@pytest.fixture(scope="module")
def _redis():
    # Instantiated once (first-write version-probe is a slow fakeredis quirk).
    return fakeredis.FakeStrictRedis()


def _mock_bootstrap():
    responses.add(
        responses.GET,
        f"{_DC_BASE}appReleaseWebService.php",
        body=json.dumps({"token": "fake_jwt_for_test", "status": "Y"}),
        status=200,
        content_type="application/json",
    )


def _mock_districts():
    responses.add(
        responses.GET,
        f"{_DC_BASE}districtWebService.php",
        body=json.dumps({"status": "Y", "districts": [
            {"dist_code": "1", "dist_name": "Central"},
            {"dist_code": "2", "dist_name": "East"},
        ]}),
        status=200,
        content_type="application/json",
    )


def _district_calls():
    return [c for c in responses.calls if "districtWebService.php" in c.request.url]


@responses.activate
def test_second_identical_call_served_from_cache_no_http(_redis):
    _redis.flushall()
    set_backend(_redis)
    _mock_bootstrap()
    _mock_districts()

    client = DistrictCourtClient()
    first = client.list_districts(state_code="26")
    second = client.list_districts(state_code="26")

    assert [d.name for d in first] == ["Central", "East"]
    assert [d.name for d in second] == ["Central", "East"]
    # The cache hit made NO second HTTP request to the districts endpoint.
    assert len(_district_calls()) == 1


@responses.activate
def test_no_backend_hits_http_every_time():
    clear_backend()
    _mock_bootstrap()
    _mock_districts()

    client = DistrictCourtClient()
    client.list_districts(state_code="26")
    client.list_districts(state_code="26")

    assert len(_district_calls()) == 2  # no cache -> two live fetches


@responses.activate
def test_cache_hit_survives_open_circuit(_redis):
    _redis.flushall()
    set_backend(_redis)
    _mock_bootstrap()
    _mock_districts()

    client = DistrictCourtClient()
    client.list_districts(state_code="26")  # warm the cache

    # Force the shared circuit OPEN (as an IP-wide throttle would).
    cb = _CircuitRegistry.get("ecourts_global", failure_threshold=5, recovery_timeout=60.0)
    for _ in range(5):
        cb.record_failure()

    # A cache MISS now hits the open circuit and fails fast...
    with pytest.raises(CircuitOpen):
        client.list_districts(state_code="99")

    # ...but the cached state is still served without touching the circuit.
    cached = client.list_districts(state_code="26")
    assert [d.name for d in cached] == ["Central", "East"]


def test_list_states_serves_static_on_throttle(monkeypatch):
    """A throttled state fetch (RateLimited) must yield the 36-state static
    snapshot on the real client, not surface an error."""
    clear_backend()
    client = DistrictCourtClient()

    def _throttled(*_a, **_k):
        raise RateLimited("eCourts 405 throttle")

    monkeypatch.setattr(client._session, "call", _throttled)

    states = client.list_states()
    assert len(states) == 36
    assert any(s.name == "Delhi" and s.code == "26" for s in states)
