"""Expected delivery slots per local day, DST aware.

Adapted from `energy-predictor` (`energy_grid/time_utils.py`). The timezone is now an argument
(taken from the zone registry) and the slot length is configurable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def delivery_slots(
    delivery_date: date, timezone: str, minutes: int = 15
) -> list[tuple[datetime, datetime]]:
    """Return the real UTC slots of a local delivery day, including DST days."""
    tz = ZoneInfo(timezone)
    cursor = datetime.combine(delivery_date, time.min, tz).astimezone(UTC)
    end = datetime.combine(delivery_date + timedelta(days=1), time.min, tz).astimezone(UTC)
    step = timedelta(minutes=minutes)
    result: list[tuple[datetime, datetime]] = []
    while cursor < end:
        result.append((cursor, cursor + step))
        cursor += step
    return result


def local_hour(timestamp: datetime, timezone: str) -> int:
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return timestamp.astimezone(ZoneInfo(timezone)).hour
