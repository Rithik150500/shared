"""Cache key builder: ``ecourts:<method>:<scope>[:<arg>...]``.

Human-readable (greppable in redis-cli) and scope-namespaced so
``DistrictCourtClient.list_states`` and ``HighCourtClient.list_states`` never
collide.
"""
from __future__ import annotations

from datetime import date
from typing import Any


def build_key(method_name: str, scope: str, key_arg_values: list[Any]) -> str:
    parts = ["ecourts", method_name, scope]
    for v in key_arg_values:
        parts.append(v.isoformat() if isinstance(v, date) else str(v))
    return ":".join(parts)
