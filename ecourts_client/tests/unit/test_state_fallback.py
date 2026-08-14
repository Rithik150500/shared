"""with_static_state_fallback: serve the baked-in state list whenever the
live list_states() errors (throttle / circuit-open / site-down) or returns
empty, so the guided-search state step can never dead-end."""
from __future__ import annotations

import pytest

from ecourts_client.errors import CircuitOpen, CourtSiteDown, ECourtsError, RateLimited
from ecourts_client.models import StateRef
from ecourts_client.state_fallback import with_static_state_fallback
from ecourts_client.static_data import STATIC_STATES


class _FakeClient:
    def __init__(self, scope: str, behavior):
        self.scope = scope
        self._behavior = behavior
        self.calls = 0

    def list_states(self):
        self.calls += 1
        return self._behavior()


def _wrap(scope, behavior):
    client = _FakeClient(scope, behavior)
    # Mirror how apply_sync_resilience wraps: decorate the unbound function,
    # then invoke with the instance as first positional.
    wrapped = with_static_state_fallback(_FakeClient.list_states)
    return client, lambda: wrapped(client)


def test_live_success_passes_through_untouched():
    live = [StateRef(code="99", name="Livestan", national_code="LV")]
    client, call = _wrap("district", lambda: live)
    assert call() == live


def test_rate_limited_serves_static():
    client, call = _wrap("district", lambda: (_ for _ in ()).throw(RateLimited("throttled")))
    result = call()
    assert result == list(STATIC_STATES["district"])
    assert len(result) == 36


def test_circuit_open_serves_static():
    client, call = _wrap("highcourt", lambda: (_ for _ in ()).throw(CircuitOpen("ecourts_global", 5.0)))
    assert call() == list(STATIC_STATES["highcourt"])


def test_site_down_serves_static():
    client, call = _wrap("district", lambda: (_ for _ in ()).throw(CourtSiteDown("down")))
    assert call() == list(STATIC_STATES["district"])


def test_empty_result_serves_static():
    client, call = _wrap("district", lambda: [])
    assert call() == list(STATIC_STATES["district"])


def test_non_ecourts_error_propagates():
    """A programming bug (ValueError) must NOT be masked by the fallback."""
    client, call = _wrap("district", lambda: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError):
        call()


def test_unknown_scope_reraises_ecourts_error():
    """No static list for the scope -> we cannot help; surface the real error."""
    client, call = _wrap("tribunal", lambda: (_ for _ in ()).throw(RateLimited("throttled")))
    with pytest.raises(ECourtsError):
        call()


def test_returns_a_fresh_list_not_the_shared_tuple():
    client, call = _wrap("district", lambda: [])
    result = call()
    assert isinstance(result, list)
    result.append("mutation")
    # The module-level constant must be untouched.
    assert len(STATIC_STATES["district"]) == 36
