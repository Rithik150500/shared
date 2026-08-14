"""The cache registry must stay consistent with the client classes and the
resilience wrap-registry: only real methods, only quasi-static list methods,
never live search/fetch methods."""
from __future__ import annotations

from ecourts_client._resilience_apply import _WRAP_REGISTRY
from ecourts_client.cache.registry import CACHE_REGISTRY
from ecourts_client.district import DistrictCourtClient
from ecourts_client.highcourt import HighCourtClient

_CLASSES = {"DistrictCourtClient": DistrictCourtClient, "HighCourtClient": HighCourtClient}


def test_all_registry_methods_exist():
    for class_name, method_name, _item_cls, _key_args in CACHE_REGISTRY:
        cls = _CLASSES[class_name]
        assert hasattr(cls, method_name), f"{class_name}.{method_name} missing"


def test_cached_methods_are_subset_of_wrap_registry():
    wrapped = {(c, m) for c, m in _WRAP_REGISTRY}
    for class_name, method_name, _ic, _ka in CACHE_REGISTRY:
        assert (class_name, method_name) in wrapped, f"{class_name}.{method_name} not resilience-wrapped"


def test_search_and_fetch_methods_not_cached():
    cached = {(c, m) for c, m, _ic, _ka in CACHE_REGISTRY}
    for c, m in cached:
        assert not m.startswith("search_by_"), f"{m} is live search -- must not be cached"
        assert not m.startswith("fetch_"), f"{m} is live fetch -- must not be cached"


def test_key_args_are_lists_of_str():
    for _cn, _mn, _ic, key_args in CACHE_REGISTRY:
        assert isinstance(key_args, list)
        assert all(isinstance(a, str) for a in key_args)
