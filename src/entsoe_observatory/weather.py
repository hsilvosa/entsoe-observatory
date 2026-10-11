"""Open-Meteo weather per zone: bronze per point, silver as a weighted zone mean.

The Spanish points are adapted from `energy-predictor` (`sources/open_meteo.py`); the
point table is now per zone (`configs/weather_points.json`). Wind speed is requested in
m/s explicitly (Open-Meteo defaults to km/h).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pyarrow as pa

from .bronze import merge_into_bronze, new_run_id, write_run_manifest
from .settings import Settings
from .silver import _connect
from .storage import swap_directory

ARCHIVE_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_LAG_DAYS = 7  # ERA5 archive is complete about 5 days behind
VARIABLES = {
    "temperature_2m": "degC",
    "wind_speed_10m": "m/s",
    "relative_humidity_2m": "%",
    "shortwave_radiation": "W/m2",
}
DEFAULT_POINTS = Path(__file__).resolve().parents[2] / "configs" / "weather_points.json"
DATASET = "weather"


@dataclass(frozen=True)
class WeatherPoint:
    name: str
    latitude: float
    longitude: float
    weight: float


def load_points(path: Path | None = None) -> dict[str, list[WeatherPoint]]:
    raw = json.loads((path or DEFAULT_POINTS).read_text(encoding="utf-8"))
    return {
        zone: [WeatherPoint(**p) for p in points] for zone, points in raw["zones"].items()
    }


def split_range(start: date, end: date, today: date) -> list[tuple[str, date, date]]:
    """Split [start, end] into an archive part and a recent forecast-API part."""
    boundary = today - timedelta(days=ARCHIVE_LAG_DAYS)
    pieces: list[tuple[str, date, date]] = []
    if start <= boundary:
        pieces.append(("archive", start, min(end, boundary)))
    if end > boundary:
        pieces.append(("forecast", max(start, boundary + timedelta(days=1)), end))
    return pieces


class WeatherClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60,
        max_retries: int = 4,
        pause_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._max_retries = max_retries
        self._pause_s = pause_s
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def fetch(
        self, points: list[WeatherPoint], source: str, start: date, end: date
    ) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
        endpoint = ARCHIVE_ENDPOINT if source == "archive" else FORECAST_ENDPOINT
        params = {
            "latitude": ",".join(str(p.latitude) for p in points),
            "longitude": ",".join(str(p.longitude) for p in points),
            "hourly": ",".join(VARIABLES),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        for attempt in range(self._max_retries + 1):
            self._sleep(self._pause_s)
            try:
                response = self._http.get(endpoint, params=params)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        f"Open-Meteo connection failed ({type(exc).__name__})"
                    ) from None
                self._sleep(2**attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                self._sleep(min(60.0, 2 ** (attempt + 1)))
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Open-Meteo request failed (HTTP {response.status_code})")
            body = response.json()
            payloads = body if isinstance(body, list) else [body]
            if len(payloads) != len(points):
                raise RuntimeError("Open-Meteo returned an unexpected number of locations")
            digest = hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()
            return payloads, digest, {"endpoint": endpoint, **params}
        raise AssertionError("unreachable")


WEATHER_SCHEMA = pa.schema(
    [
        ("zone_key", pa.string()),
        ("point", pa.string()),
        ("weight", pa.float64()),
        ("timestamp_utc", pa.timestamp("us", tz="UTC")),
        *[(name, pa.float64()) for name in VARIABLES],
        ("source", pa.string()),
        ("source_sha256", pa.string()),
        ("run_id", pa.string()),
        ("ingested_at_utc", pa.timestamp("us", tz="UTC")),
    ]
)


def parse_payloads(
    zone_key: str,
    points: list[WeatherPoint],
    payloads: list[dict[str, Any]],
    *,
    source: str,
    sha256: str,
    run_id: str,
    ingested_at: datetime,
) -> pa.Table:
    cols: dict[str, list[Any]] = {name: [] for name in WEATHER_SCHEMA.names}
    for point, payload in zip(points, payloads, strict=True):
        hourly = payload["hourly"]
        for index, stamp in enumerate(hourly["time"]):
            cols["zone_key"].append(zone_key)
            cols["point"].append(point.name)
            cols["weight"].append(point.weight)
            cols["timestamp_utc"].append(datetime.fromisoformat(stamp).replace(tzinfo=UTC))
            for name in VARIABLES:
                value = hourly[name][index]
                cols[name].append(None if value is None else float(value))
            cols["source"].append(source)
            cols["source_sha256"].append(sha256)
            cols["run_id"].append(run_id)
            cols["ingested_at_utc"].append(ingested_at)
    return pa.table(cols, schema=WEATHER_SCHEMA)


SILVER_SQL = """
WITH ranked AS (
    SELECT *, row_number() OVER (
        PARTITION BY point, timestamp_utc
        ORDER BY (source = 'archive') DESC, ingested_at_utc DESC
    ) AS rn
    FROM bronze_weather
)
SELECT
    zone_key,
    timestamp_utc,
    {means},
    count(*) FILTER (WHERE temperature_2m IS NOT NULL) AS points_used,
    count(*) AS points_total,
    min(source) AS source,
    CAST(year(timestamp_utc) AS VARCHAR) AS year,
    lpad(CAST(month(timestamp_utc) AS VARCHAR), 2, '0') AS month
FROM ranked WHERE rn = 1
GROUP BY zone_key, timestamp_utc
ORDER BY timestamp_utc
"""


def build_weather_silver(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    zone_key: str,
    months: set[tuple[str, str]],
) -> int:
    """Weighted zone mean per hour for the given months (weights renormalised over non-nulls)."""
    zone_dir = settings.layer_dir("bronze", DATASET) / f"zone={zone_key}"
    patterns = [
        (zone_dir / f"year={y}" / f"month={m}" / "*.parquet").as_posix() for y, m in sorted(months)
    ]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE bronze_weather AS "
        f"SELECT * FROM read_parquet({patterns!r}, hive_partitioning=false)"
    )
    means = ", ".join(
        f"sum({v} * weight) / nullif(sum(weight) FILTER (WHERE {v} IS NOT NULL), 0) AS {v}"
        for v in VARIABLES
    )
    final_dir = settings.layer_dir("silver", DATASET) / f"zone={zone_key}"
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({SILVER_SQL.format(means=means)}) TO '{tmp_dir.as_posix()}' "
        "(FORMAT parquet, COMPRESSION zstd, PARTITION_BY (year, month), OVERWRITE_OR_IGNORE)"
    )
    for year, month in sorted(months):
        built = tmp_dir / f"year={year}" / f"month={month}"
        if built.exists():
            swap_directory(built, final_dir / f"year={year}" / f"month={month}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    row = con.execute(
        "SELECT count(DISTINCT (point, timestamp_utc)) FROM bronze_weather"
    ).fetchone()
    return int(row[0]) if row else 0


def pull_weather(
    settings: Settings,
    client: WeatherClient,
    *,
    start: date,
    end: date,
    zones: list[str] | None = None,
    run_id: str | None = None,
    today: date | None = None,
    points_path: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    today = today or datetime.now(UTC).date()
    all_points = load_points(points_path)
    con = _connect(settings.threads)
    ingested_at = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    for zone_key in zones or sorted(all_points):
        points = all_points[zone_key]
        months: set[tuple[str, str]] = set()
        rows = 0
        for source, piece_start, piece_end in split_range(start, end, today):
            payloads, digest, _ = client.fetch(points, source, piece_start, piece_end)
            table = parse_payloads(
                zone_key, points, payloads, source=source, sha256=digest, run_id=run_id,
                ingested_at=ingested_at,
            )
            merged = merge_into_bronze(settings, DATASET, zone_key, table)
            months.update((y, m) for y, m in merged["months"])
            rows += merged["rows"]
        if months:
            build_weather_silver(con, settings, zone_key, months)
        results.append({"zone": zone_key, "rows": rows, "months": len(months)})
        if on_progress:
            on_progress(f"weather/{zone_key}: rows={rows}")
    summary = {"start": start.isoformat(), "end": end.isoformat(), "zones": results,
               "rows": sum(r["rows"] for r in results)}
    write_run_manifest(settings, run_id, "weather", summary)
    return summary
