"""with_cache_sync: Redis read-through around a picker method.

Semantics: pass-through when no backend; miss -> live + SETEX; hit -> skip inner;
empties never cached; every Redis failure fails OPEN (serve live). Fakeredis
stands in for a real client (duck-typed .get/.setex)."""
from __future__ import annotations

import fakeredis
import pytest

from ecourts_client.cache.backend import clear_backend, set_backend
from ecourts_client.cache.decorator import with_cache_sync
from ecourts_client.models import DistrictRef


# One fakeredis instance per module: its first WRITE pays a ~10s version-probe
# on some local setups (a fakeredis quirk, not our code -- CI is unaffected), so
# we pay it once and flush between tests instead of per-instance.
@pytest.fixture(scope="module")
def _shared_redis():
    return fakeredis.FakeStrictRedis()


@pytest.fixture(autouse=True)
def _reset(_shared_redis):
    _shared_redis.flushall()
    clear_backend()
    yield
    clear_backend()


class _FakeClient:
    scope = "district"

    def __init__(self, rows=None):
        self.calls = 0
        self._rows = rows if rows is not None else [DistrictRef(code="7", name="Central", state_code="26")]

    def list_districts(self, state_code):
        self.calls += 1
        return list(self._rows)


_wrapped = with_cache_sync(item_cls=DistrictRef, key_args=["state_code"], ttl_seconds=3600)(
    _FakeClient.list_districts
)


def _call(client, **kw):
    return _wrapped(client, **kw)


def test_no_backend_is_passthrough():
    c = _FakeClient()
    assert _call(c, state_code="26")[0].name == "Central"
    assert c.calls == 1  # inner ran; nothing cached (no backend)


def test_miss_then_set_then_hit_skips_inner(_shared_redis):
    set_backend(_shared_redis)
    c = _FakeClient()
    first = _call(c, state_code="26")
    assert c.calls == 1
    second = _call(c, state_code="26")
    assert second == first
    assert c.calls == 1  # hit -> inner NOT called again


def test_hit_deserializes_to_dataclasses(_shared_redis):
    set_backend(_shared_redis)
    c = _FakeClient()
    _call(c, state_code="26")
    out = _call(c, state_code="26")
    assert all(isinstance(r, DistrictRef) for r in out)
    assert out[0].state_code == "26"


def test_empty_result_not_cached(_shared_redis):
    set_backend(_shared_redis)
    c = _FakeClient(rows=[])
    _call(c, state_code="26")
    _call(c, state_code="26")
    assert c.calls == 2  # empty never cached -> inner runs every time


def test_key_includes_scope_and_args(_shared_redis):
    set_backend(_shared_redis)
    _call(_FakeClient(), state_code="26")
    keys = {k.decode() for k in _shared_redis.keys("*")}
    assert "ecourts:list_districts:district:26" in keys


def test_cached_entry_written_with_ttl(_shared_redis):
    """The 24h staleness bound the whole design rests on requires entries to
    carry an expiry -- a missing TTL would cache picker lists forever."""
    set_backend(_shared_redis)
    _call(_FakeClient(), state_code="26")
    ttl = _shared_redis.ttl("ecourts:list_districts:district:26")
    assert ttl is not None and ttl > 0  # -1 (no expire) / -2 (no key) would fail


def test_get_error_falls_through_to_live():
    class _BadGet:
        def get(self, key):
            raise RuntimeError("redis down")

        def setex(self, *a):
            pass

    set_backend(_BadGet())
    c = _FakeClient()
    assert _call(c, state_code="26")[0].name == "Central"
    assert c.calls == 1  # served live despite GET failure


def test_set_error_returns_live_result():
    class _BadSet:
        def get(self, key):
            return None

        def set(self, *a, **k):
            raise RuntimeError("redis full")

    set_backend(_BadSet())
    c = _FakeClient()
    assert _call(c, state_code="26")[0].name == "Central"  # SET failure swallowed


def test_corrupt_cache_falls_through_and_overwrites(_shared_redis):
    _shared_redis.set("ecourts:list_districts:district:26", b"{not valid json")
    set_backend(_shared_redis)
    c = _FakeClient()
    out = _call(c, state_code="26")
    assert out[0].name == "Central"
    assert c.calls == 1  # corrupt entry -> live fetch
    # self-heals: the key now holds valid JSON
    assert _shared_redis.get("ecourts:list_districts:district:26") != b"{not valid json"
