"""Which client methods get the read-through cache, and how their keys are built.

Each entry: ``(class_name, method_name, item_cls, key_args)`` where ``key_args``
are the method parameter names whose values namespace the cache key. Only
quasi-static list/picker methods belong here -- never ``search_by_*`` /
``fetch_*`` (live data).

Consistency is asserted by tests/unit/test_cache_registry.py (methods exist,
subset of the resilience wrap-registry, no live methods).
"""
from ecourts_client.models import (
    BenchRef,
    CaseTypeRef,
    CourtComplexRef,
    DistrictRef,
    PoliceStationRef,
    StateRef,
)

# (class_name, method_name, item_cls, key_args)
CACHE_REGISTRY: list[tuple[str, str, type, list[str]]] = [
    ("DistrictCourtClient", "list_states",         StateRef,         []),
    ("DistrictCourtClient", "list_districts",       DistrictRef,      ["state_code"]),
    ("DistrictCourtClient", "list_court_complexes", CourtComplexRef,  ["state_code", "district_code"]),
    ("DistrictCourtClient", "list_case_types",      CaseTypeRef,      ["state_code", "district_code", "court_code"]),
    ("DistrictCourtClient", "list_police_stations", PoliceStationRef, ["state_code", "district_code", "court_code"]),
    ("HighCourtClient",     "list_states",          StateRef,         []),
    ("HighCourtClient",     "list_hc_benches",      BenchRef,         ["state_code"]),
    ("HighCourtClient",     "list_case_types",      CaseTypeRef,      ["state_code", "district_code", "court_code"]),
    # NOTE: list_bench_sittings (date-keyed cause-list helper) is deliberately
    # NOT cached. The 2026-05-27 design included it, but its only production
    # caller is the nightly cause-list indexer, which queries each date exactly
    # once -- so a 24h cache yields ZERO hits while exposing the (intra-day
    # mutable) bench roster to up-to-TTL staleness. All upside, no benefit:
    # excluded so the cache touches only genuine picker/reference lists.
]
