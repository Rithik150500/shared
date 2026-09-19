"""JSON (de)serialization for cached picker lists.

Cached values are lists of FLAT frozen dataclasses (StateRef, DistrictRef,
CourtComplexRef, PoliceStationRef, CaseTypeRef, BenchRef, HCBenchSitting) --
str/int fields plus at most one ``date`` (HCBenchSitting.sitting_date). We
store JSON (not pickle) so entries are inspectable in ``redis-cli`` and immune
to class-layout drift.
"""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import date
from typing import Any, Type


def to_json(items: list[Any]) -> str:
    return json.dumps([_asdict(it) for it in items], default=_encode)


def _encode(o: Any) -> Any:
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"not JSON-serializable: {type(o)}")


def _asdict(obj: Any) -> dict[str, Any]:
    # Shallow -- cached dataclasses are flat (no nested dataclasses).
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


def from_json(payload: str, item_cls: Type[Any]) -> list[Any]:
    raw = json.loads(payload)
    # models.py uses ``from __future__ import annotations``, so ``f.type`` is the
    # literal string "date" (PEP 563), not the ``date`` object -- match both.
    date_field_names = {f.name for f in fields(item_cls) if f.type in (date, "date")}
    out = []
    for d in raw:
        kwargs = dict(d)
        for name in date_field_names:
            if kwargs.get(name) is not None:
                kwargs[name] = date.fromisoformat(kwargs[name])
        out.append(item_cls(**kwargs))
    return out
