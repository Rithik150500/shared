"""Coverage targets and aggregation for the canary suite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def report_coverage(canaries_path: Path) -> dict[str, Any]:
    """Return a coverage report: target vs. filled per category."""
    data = json.loads(canaries_path.read_text(encoding="utf-8"))
    canaries = data.get("canaries", [])
    targets = data.get("coverage_targets", [])

    states = {c.get("state") for c in canaries if c.get("state")}
    districts = {c.get("district") for c in canaries if c.get("district")}
    high_courts = {c.get("bench") for c in canaries if c.get("scope") == "highcourt"}

    return {
        "states": {"target": _target_for("states", targets), "filled": len(states)},
        "districts": {"target": _target_for("districts", targets), "filled": len(districts)},
        "high_courts": {"target": _target_for("high_courts", targets), "filled": len(high_courts)},
        "total_canaries": len(canaries),
    }


def _target_for(name: str, targets: list[dict[str, Any]]) -> int:
    for t in targets:
        if t["category"] == name:
            return int(t["target"])
    return 0
