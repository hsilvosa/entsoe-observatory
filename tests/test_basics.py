from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from entsoe_observatory.a03 import expand_positions
from entsoe_observatory.time_utils import delivery_slots, local_hour
from entsoe_observatory.zones import load_registry


def test_a03_repeats_previous_value_until_period_end() -> None:
    rows = list(expand_positions({1: 10.0, 4: 20.0}, 6, compressed=True))
    assert rows == [
        (1, 10.0, False),
        (2, 10.0, True),
        (3, 10.0, True),
        (4, 20.0, False),
        (5, 20.0, True),
        (6, 20.0, True),
    ]


def test_non_compressed_curve_keeps_gaps() -> None:
    rows = list(expand_positions({1: 10.0, 4: 20.0}, 6, compressed=False))
    assert [r[0] for r in rows] == [1, 4]


def test_missing_first_position_is_not_invented() -> None:
    rows = list(expand_positions({3: 5.0}, 4, compressed=True))
    assert rows == [(3, 5.0, False), (4, 5.0, True)]


@pytest.mark.parametrize(
    ("day", "expected"),
    [(date(2025, 3, 30), 92), (date(2025, 10, 26), 100), (date(2025, 6, 1), 96)],
)
def test_dst_quarter_hour_counts(day: date, expected: int) -> None:
    assert len(delivery_slots(day, "Europe/Madrid", 15)) == expected


def test_registry_is_complete_and_timezones_valid() -> None:
    registry = load_registry()
    assert len(registry) == 51
    for zone in registry.values():
        assert delivery_slots(date(2025, 6, 1), zone.timezone, 60)


def test_calendar_uses_market_timezone() -> None:
    """Ported from energy-predictor: the local hour follows the zone timezone."""
    registry = load_registry()
    instant = datetime(2025, 1, 6, 0, 0, tzinfo=UTC)
    assert local_hour(instant, registry["ES"].timezone) == 1
    assert local_hour(instant, registry["PT"].timezone) == 0


def test_calendar_handles_daylight_saving_transition() -> None:
    """Ported from energy-predictor: 00:30Z and 01:30Z map to 01h and 03h on the DST day."""
    tz = load_registry()["ES"].timezone
    hours = [
        local_hour(datetime(2025, 3, 30, 0, 30, tzinfo=UTC), tz),
        local_hour(datetime(2025, 3, 30, 1, 30, tzinfo=UTC), tz),
    ]
    assert hours == [1, 3]
