"""Cache key shape: ecourts:<method>:<scope>[:<arg>...] -- human-readable and
scope-namespaced so DC and HC never collide on same-named methods."""
from __future__ import annotations

from datetime import date

from ecourts_client.cache.keys import build_key


def test_no_args_key():
    assert build_key("list_states", "district", []) == "ecourts:list_states:district"


def test_single_arg_key():
    assert build_key("list_districts", "district", ["7"]) == "ecourts:list_districts:district:7"


def test_multi_arg_key():
    assert build_key("list_case_types", "highcourt", ["1", "1", "1"]) == "ecourts:list_case_types:highcourt:1:1:1"


def test_date_arg_isoformatted():
    key = build_key("list_bench_sittings", "highcourt", ["1", "1", "1", date(2026, 7, 16)])
    assert key.endswith(":2026-07-16")


def test_scope_prevents_dc_hc_collision():
    dc = build_key("list_states", "district", [])
    hc = build_key("list_states", "highcourt", [])
    assert dc != hc
