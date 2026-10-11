"""Phase 0 probes for reusable energy-predictor pieces (scratch only)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from energy_grid.domain import EventType, GridEvent
from energy_grid.sources.dataset_reader import EntsoeDatasetReader
from energy_grid.sources.entsoe import parse_entsoe_document
from energy_grid.time_utils import delivery_intervals, floor_quarter_hour, interval_number

DATA = Path(r"D:\datasets\entsoe-transparency")


@pytest.mark.parametrize(
    ("day", "expected"),
    [(date(2025, 3, 30), 92), (date(2025, 10, 26), 100), (date(2025, 6, 12), 96)],
)
def test_delivery_intervals_dst(day: date, expected: int) -> None:
    assert len(delivery_intervals(day)) == expected


def test_interval_number_and_floor() -> None:
    ts = datetime(2025, 10, 26, 1, 0, tzinfo=UTC)  # 03:00 CEST -> second 02:00 local hour
    assert interval_number(ts) == 13
    assert floor_quarter_hour(datetime(2025, 1, 1, 10, 7, 59, tzinfo=UTC)).minute == 0


A03_DOC = b"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
 <TimeSeries><curveType>A03</curveType><Period>
  <timeInterval><start>2024-03-30T23:00Z</start><end>2024-03-31T03:00Z</end></timeInterval>
  <resolution>PT60M</resolution>
  <Point><position>1</position><price.amount>10</price.amount></Point>
  <Point><position>3</position><price.amount>20</price.amount></Point>
 </Period></TimeSeries></Publication_MarketDocument>"""


def test_entsoe_parser_a03_expands_compressed_points() -> None:
    """A03 omits positions that repeat the previous value; 4 hours must yield 4 events."""
    events = list(parse_entsoe_document(A03_DOC, EventType.PRICE))
    assert [e.value for e in events] == [10, 10, 20, 20]


def test_entsoe_parser_on_real_raw_xml() -> None:
    path = DATA / "data/raw/day_ahead_prices/ES_20240301_20240401.xml"
    events = list(parse_entsoe_document(path.read_bytes(), EventType.PRICE))
    assert events and all(e.unit == "EUR/MWh" for e in events)


def test_dataset_reader_real_snapshot_mixed_resolution() -> None:
    reader = EntsoeDatasetReader(DATA / "data" / "processed")
    frame = reader.load_raw_table(
        "day_ahead_prices", country_code="ES", start_time=datetime(2025, 9, 30, tzinfo=UTC),
        end_time=datetime(2025, 10, 2, tzinfo=UTC),
    )
    assert set(frame["resolution"]) >= {"PT60M", "PT15M"}


def test_dataset_reader_drops_rows_from_parallel_sequences() -> None:
    """DE_LU publishes two classification sequences; dedup on timestamp alone loses one."""
    reader = EntsoeDatasetReader(DATA / "data" / "processed")
    frame = reader.load_raw_table(
        "day_ahead_prices", country_code="DE", zone_key="DE_LU",
        start_time=datetime(2024, 1, 1, tzinfo=UTC), end_time=datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert frame["timestamp_utc"].is_unique  # passes, but hides that two series were merged


def test_grid_event_matches_json_schema() -> None:
    schema = json.loads(Path("grid-event-v1.json").read_text())
    event = GridEvent(
        source="t", event_type=EventType.PRICE, interval_start=datetime(2025, 1, 1, tzinfo=UTC),
        interval_end=datetime(2025, 1, 1, 1, tzinfo=UTC), published_at=datetime.now(UTC),
        value=1.0, unit="EUR/MWh",
    )
    dumped = json.loads(event.model_dump_json())
    assert set(dumped) == set(schema["properties"])
    assert set(schema["required"]) <= set(dumped)


def test_price_unit_is_not_always_eur() -> None:
    """Snapshot has RON/BGN/PLN prices; domain only accepts EUR/MWh."""
    with pytest.raises(ValueError):
        GridEvent(
            source="t", event_type=EventType.PRICE,
            interval_start=datetime(2025, 1, 1, tzinfo=UTC),
            interval_end=datetime(2025, 1, 1, 1, tzinfo=UTC), published_at=datetime.now(UTC),
            value=1.0, unit="PLN/MWh",
        )
