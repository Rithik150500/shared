"""Parsers for the geographic-hierarchy dropdown endpoints.

Each endpoint returns a JSON object with one top-level collection of rows:
    stateWebService.php       -> {"states": ...}
    districtWebService.php    -> {"districts": ...}
    courtEstWebService.php    -> {"courtComplex": ...}

Historically (pre-2026) the value was a JSON LIST of NIC-typed dicts. On
2026-05-13 we observed the states endpoint return a DICT keyed by
`state_code` (as string) with the same row shape as values. The other two
endpoints may follow the same migration. To stay forward + backward
compatible we normalise both shapes into a flat list of rows via
`_normalise_rows` before projecting to the minimal `*Ref` dataclasses
defined in models.py.
"""
from __future__ import annotations

from typing import Any

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import CourtComplexRef, DistrictRef, StateRef


def _normalise_rows(raw: Any, *, field: str) -> list[dict[str, Any]]:
    """Accept either a list of rows (legacy shape) or a dict-of-rows
    keyed by the row's primary code (post-2026 shape) and return a flat
    list. Raises SchemaChanged if the input is neither.

    The eCourts NIC backend appears to be migrating its dropdown endpoints
    from list to dict shape; the row content (state_code / dist_code /
    complex_code etc.) is unchanged, so callers can keep their existing
    projection logic against the returned list.
    """
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    raise SchemaChanged(field=field, reason=f"expected list or dict, got {type(raw).__name__}")


def parse_states(response: dict[str, Any]) -> list[StateRef]:
    rows = _normalise_rows(response.get("states"), field="states")
    return [
        StateRef(
            code=str(r["state_code"]),
            name=(r.get("state_name") or "").strip(),
            national_code=(r.get("nationalstate_code") or "").strip(),
        )
        for r in rows
        if r.get("state_code") is not None and r.get("display") != "N"
    ]


def parse_districts(response: dict[str, Any], state_code: str) -> list[DistrictRef]:
    rows = _normalise_rows(response.get("districts"), field="districts")
    return [
        DistrictRef(
            code=str(r["dist_code"]),
            name=(r.get("dist_name") or "").strip(),
            state_code=state_code,
        )
        for r in rows
        if r.get("dist_code") is not None and r.get("display") != "N"
    ]


def parse_court_complexes(response: dict[str, Any], state_code: str, district_code: str) -> list[CourtComplexRef]:
    rows = _normalise_rows(response.get("courtComplex"), field="courtComplex")
    return [
        CourtComplexRef(
            code=str(r["complex_code"]),
            name=(r.get("court_complex_name") or "").strip(),
            state_code=state_code,
            district_code=district_code,
            est_code=str(r.get("njdg_est_code") or ""),
        )
        for r in rows
        if r.get("complex_code")
    ]
