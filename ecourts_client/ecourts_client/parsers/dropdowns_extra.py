"""Parsers for the secondary dropdown endpoints (police stations, case types, HC benches).

These three responses use a quirky encoding compared to states/districts/complexes:
the entire list is packed into a single `#`-delimited string with `~` separating
code from name. We parse it back into typed dataclasses.
"""
from __future__ import annotations

from typing import Any

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import BenchRef, CaseTypeRef, PoliceStationRef


def _parse_hash_list(packed: str) -> list[tuple[str, str]]:
    """Split 'code1~name1#code2~name2#...' into [(code, name), ...]."""
    if not packed:
        return []
    out: list[tuple[str, str]] = []
    for entry in packed.split("#"):
        if not entry:
            continue
        if "~" not in entry:
            continue
        code, name = entry.split("~", 1)
        out.append((code.strip(), name.strip()))
    return out


def parse_police_stations(
    response: dict[str, Any], *, district_code: str, court_code: str
) -> list[PoliceStationRef]:
    rows = response.get("police_stationlist")
    if not isinstance(rows, list) or not rows:
        return []
    # The list typically holds one establishment row whose `police_station` packs every
    # station in the jurisdiction. uniform_code maps station-code -> national uniform id.
    out: list[PoliceStationRef] = []
    for row in rows:
        packed = row.get("police_station") or ""
        uniform_map = row.get("uniform_code") or {}
        if not isinstance(uniform_map, dict):
            uniform_map = {}
        for code, name in _parse_hash_list(packed):
            try:
                uniform = int(uniform_map.get(code, 0) or 0)
            except (TypeError, ValueError):
                uniform = 0
            out.append(PoliceStationRef(
                code=code, name=name,
                district_code=district_code, court_code=court_code,
                uniform_code=uniform,
            ))
    return out


def parse_case_types(response: dict[str, Any], *, court_code: str) -> list[CaseTypeRef]:
    rows = response.get("case_types")
    if not isinstance(rows, list) or not rows:
        return []
    out: list[CaseTypeRef] = []
    for row in rows:
        packed = row.get("case_type") or ""
        for code, name in _parse_hash_list(packed):
            out.append(CaseTypeRef(code=code, name=name, court_code=court_code))
    return out


def parse_hc_benches(response: dict[str, Any], *, state_code: str) -> list[BenchRef]:
    """HC benches come back via districtWebService.php with action_code='benches';
    the response key is `districts` even though the rows are benches.
    """
    rows = response.get("districts")
    if not isinstance(rows, list):
        raise SchemaChanged(field="districts(benches)", reason=f"expected list, got {type(rows).__name__}")
    return [
        BenchRef(
            code=str(r["dist_code"]),
            name=(r.get("dist_name") or "").strip(),
            state_code=state_code,
        )
        for r in rows
        if r.get("dist_code") is not None and r.get("display") != "N"
    ]
