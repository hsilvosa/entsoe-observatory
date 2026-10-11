from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from entsoe_observatory.events import (
    EventType,
    GridEventV2,
    event_from_silver_row,
    resolve_events,
    source_snapshot_id,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "grid-event-v2.json"


def price(
    value: float, *, revision: int = 1, published: datetime = START, **kw: object
) -> GridEventV2:
    fields: dict[str, object] = {
        "source": "test", "event_type": EventType.PRICE, "area": "ES",
        "interval_start": START, "interval_end": START + timedelta(hours=1),
        "resolution": "PT60M", "published_at": published, "value": value,
        "unit": "EUR/MWh", "currency": "EUR", "revision": revision,
        "payload_checksum": "a",
    }
    fields.update(kw)
    return GridEventV2(**fields)  # type: ignore[arg-type]


def test_newer_revision_wins() -> None:
    first = price(100.0)
    revised = price(110.0, revision=2)
    assert [e.value for e in resolve_events([first, revised])] == [110.0]
    assert [e.value for e in resolve_events([revised, first])] == [110.0]


def test_late_revision_is_cut_off_at_snapshot_time() -> None:
    original = price(10.0, published=START - timedelta(minutes=5))
    late = price(99.0, revision=2, published=START + timedelta(minutes=5))
    assert [e.value for e in resolve_events([original, late], as_of=START)] == [10.0]


def test_resolutions_and_sequences_are_not_merged() -> None:
    hourly = price(1.0)
    quarter = price(2.0, resolution="PT15M", interval_end=START + timedelta(minutes=15))
    second_sequence = price(3.0, sequence=2)
    assert len(resolve_events([hourly, quarter, second_sequence])) == 3


def test_snapshot_id_is_stable_and_order_independent() -> None:
    a = [price(1.0, payload_checksum="a")]
    b = [price(2.0, payload_checksum="b")]
    assert source_snapshot_id(a, b) == source_snapshot_id(b, a)
    assert source_snapshot_id(a, b).startswith("snap-")


def test_contract_is_currency_aware_and_has_no_default_area() -> None:
    assert price(1.0, unit="RON/MWh", currency="RON").currency == "RON"
    with pytest.raises(ValidationError):
        price(1.0, unit="EUR/MWh", currency="RON")
    with pytest.raises(ValidationError):
        price(1.0, currency=None)
    with pytest.raises(ValidationError):
        GridEventV2(  # type: ignore[call-arg]
            source="t", event_type=EventType.DEMAND, interval_start=START,
            interval_end=START + timedelta(hours=1), published_at=START, value=1.0, unit="MW",
        )


def test_event_from_silver_row_is_deterministic() -> None:
    row = {
        "dataset": "actual_load", "zone_key": "PT", "timestamp_utc": START,
        "resolution": "PT15M", "resolution_minutes": 15, "sequence": 0, "value": 5000.0,
        "unit": "MW", "currency": None, "is_a03_filled": True, "is_valid": True,
        "revision_number": 1,
    }
    one, two = event_from_silver_row(row), event_from_silver_row(row)
    assert one.event_id == two.event_id
    assert one.event_type == EventType.DEMAND and one.is_a03_filled


def test_committed_json_schema_matches_the_model() -> None:
    committed = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert committed == GridEventV2.model_json_schema()
