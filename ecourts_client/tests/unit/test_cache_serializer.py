"""JSON (de)serialization for the picker cache. Flat dataclasses only; the one
date field (HCBenchSitting.sitting_date) round-trips as ISO. Debuggable in
redis-cli."""
from __future__ import annotations

import json
from datetime import date

from ecourts_client.cache.serializer import from_json, to_json
from ecourts_client.models import HCBenchSitting, PoliceStationRef, StateRef


def test_roundtrip_plain_dataclass():
    items = [
        StateRef(code="26", name="Delhi", national_code="DL"),
        StateRef(code="1", name="Maharashtra", national_code="MH"),
    ]
    assert from_json(to_json(items), StateRef) == items


def test_roundtrip_date_field():
    items = [HCBenchSitting(code="5", name="Principal", state_code="1", sitting_date=date(2026, 7, 16))]
    restored = from_json(to_json(items), HCBenchSitting)
    assert restored == items
    assert restored[0].sitting_date == date(2026, 7, 16)


def test_date_serialized_as_iso_string():
    payload = to_json([HCBenchSitting(code="5", name="P", state_code="1", sitting_date=date(2026, 7, 16))])
    assert json.loads(payload)[0]["sitting_date"] == "2026-07-16"


def test_roundtrip_int_field_preserved():
    items = [PoliceStationRef(code="3", name="Kotwali", district_code="7", court_code="1", uniform_code=42)]
    restored = from_json(to_json(items), PoliceStationRef)
    assert restored == items
    assert restored[0].uniform_code == 42


def test_empty_list_roundtrip():
    assert from_json(to_json([]), StateRef) == []
