"""Zone registry: bidding zones with timezone, currencies and validity window."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel

DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "configs" / "zones.json"


class Zone(BaseModel):
    key: str
    name: str
    country_code: str
    eic_code: str
    timezone: str
    currencies: list[str]
    valid_from: date | None = None
    valid_to: date | None = None


def load_registry(path: Path | None = None) -> dict[str, Zone]:
    raw = json.loads((path or DEFAULT_REGISTRY).read_text(encoding="utf-8"))
    zones = [Zone.model_validate(item) for item in raw["zones"]]
    return {zone.key: zone for zone in zones}
