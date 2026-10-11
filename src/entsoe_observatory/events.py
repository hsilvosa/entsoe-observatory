"""Event contract v2 and revision helpers.

Adapted from `energy-predictor` (`domain.py`, `schemas/grid-event-v1.json`, and
`live_pipeline.py`: `events_to_series`, `weather_to_frame`, `source_snapshot_id`). Changes:

- `area` is required (no default zone) and is a zone key of the registry.
- Units are currency aware (`EUR/MWh`, `RON/MWh`, ...) and tied to a `currency` field.
- Native resolution and price sequence are part of the event and of its business key.
- Revision resolution works on events at native resolution; nothing is resampled to 15 min.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "2.0"
EVENT_NAMESPACE = uuid.UUID("6b1f1b6e-6c0f-4a63-9f43-5d6f0d3f7a11")
PRICE_UNIT = re.compile(r"^[A-Z]{3}/MWh$")
WEATHER_UNITS = {"degC", "m/s", "%", "W/m2"}


class EventType(StrEnum):
    DEMAND = "demand"
    PRICE = "price"
    WEATHER = "weather"
    FX = "fx"


class QualityStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    ESTIMATED = "estimated"
    INVALID = "invalid"


class GridEventV2(BaseModel):
    """Versioned canonical event shared by producers and consumers of the gold layer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid.uuid4)
    source: str = Field(min_length=1)
    event_type: EventType
    area: str = Field(min_length=1)
    interval_start: datetime
    interval_end: datetime
    resolution: str | None = None
    sequence: int = Field(default=0, ge=0)
    published_at: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    value: float | None
    unit: str
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    revision: int = Field(default=1, ge=1)
    quality_status: QualityStatus = QualityStatus.VALID
    is_a03_filled: bool = False
    dimension: str | None = None
    payload_checksum: str | None = None

    @field_validator("interval_start", "interval_end", "published_at", "ingested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_event(self) -> GridEventV2:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must be after interval_start")
        if self.event_type == EventType.DEMAND and self.unit != "MW":
            raise ValueError(f"unit {self.unit!r} is invalid for demand")
        if self.event_type == EventType.PRICE:
            if self.currency is None:
                raise ValueError("price events need a currency")
            if not PRICE_UNIT.match(self.unit) or self.unit != f"{self.currency}/MWh":
                raise ValueError(f"unit {self.unit!r} does not match currency {self.currency!r}")
        if self.event_type == EventType.WEATHER and self.unit not in WEATHER_UNITS:
            raise ValueError(f"unit {self.unit!r} is invalid for weather")
        if self.event_type == EventType.FX and (self.currency is None or self.unit != "per_EUR"):
            raise ValueError("fx events need a currency and unit per_EUR")
        if self.quality_status == QualityStatus.MISSING and self.value is not None:
            raise ValueError("missing events must have a null value")
        if self.quality_status == QualityStatus.VALID and self.value is None:
            raise ValueError("valid events must have a value")
        return self

    @property
    def business_key(self) -> str:
        parts = (
            self.source,
            self.event_type,
            self.area,
            self.interval_start.isoformat(),
            self.resolution or "",
            self.sequence,
            self.dimension or "",
            self.currency or "",
        )
        return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()

    def to_message(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_message(cls, value: bytes | str) -> GridEventV2:
        return cls.model_validate_json(value)


def event_from_silver_row(
    row: dict[str, Any], *, source: str = "entsoe", published_at: datetime | None = None
) -> GridEventV2:
    """Build a deterministic event from a silver row of prices or load."""
    from datetime import timedelta

    start = row["timestamp_utc"].astimezone(UTC)
    minutes = int(row["resolution_minutes"])
    is_price = row["dataset"] == "day_ahead_prices"
    revision = int(row.get("revision_number") or 1)
    valid = bool(row["is_valid"])
    event = GridEventV2(
        event_id=uuid.uuid5(
            EVENT_NAMESPACE,
            f"{row['dataset']}|{row['zone_key']}|{start.isoformat()}|{row['resolution']}"
            f"|{row['sequence']}|{revision}",
        ),
        source=source,
        event_type=EventType.PRICE if is_price else EventType.DEMAND,
        area=row["zone_key"],
        interval_start=start,
        interval_end=start + timedelta(minutes=minutes),
        resolution=row["resolution"],
        sequence=int(row["sequence"]),
        published_at=published_at or start,
        value=float(row["value"]) if valid else None,
        unit=row["unit"],
        currency=row["currency"] if is_price else None,
        revision=revision,
        quality_status=QualityStatus.VALID if valid else QualityStatus.INVALID,
        is_a03_filled=bool(row["is_a03_filled"]),
    )
    return event


def resolve_events(
    events: Iterable[GridEventV2], *, as_of: datetime | None = None
) -> list[GridEventV2]:
    """Latest revision per business key. With `as_of`, later publications are ignored."""
    cutoff = as_of.astimezone(UTC) if as_of else None
    best: dict[str, GridEventV2] = {}
    for event in events:
        if cutoff is not None and event.published_at > cutoff:
            continue
        key = event.business_key
        current = best.get(key)
        rank = (event.revision, event.published_at, event.ingested_at)
        if current is None or rank > (current.revision, current.published_at, current.ingested_at):
            best[key] = event
    return sorted(best.values(), key=lambda e: (e.area, e.event_type, e.interval_start))


def source_snapshot_id(*event_groups: Iterable[GridEventV2]) -> str:
    """Stable id of the set of payloads behind a dataset snapshot."""
    checksums = sorted(
        {e.payload_checksum for group in event_groups for e in group if e.payload_checksum}
    )
    raw = "|".join(checksums).encode()
    return f"snap-{hashlib.sha256(raw).hexdigest()[:20]}"
