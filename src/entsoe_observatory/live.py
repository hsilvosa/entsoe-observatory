"""Incremental live ingestion of ENTSO-E prices and load.

For every (dataset, zone) the window starts a few days before the newest stored timestamp
(lookback for late revisions) and ends after the next publication horizon. The raw XML is
kept in landing with its checksum, merged into the affected bronze months and silver is
rebuilt for those months only. A newer revision wins in silver; re-ingesting an identical
payload changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Protocol

import duckdb
import pyarrow as pa

from .bronze import merge_into_bronze, new_run_id, write_run_manifest
from .client import EntsoeApiError, FetchResult
from .parser import DATASETS, PRICES, parse_document
from .settings import Settings
from .silver import _connect, build_silver_zone
from .zones import Zone, load_registry

LIVE_MANIFEST = "live_manifest.jsonl"


class Fetcher(Protocol):
    def fetch(self, dataset: str, zone: Zone, start: datetime, end: datetime) -> FetchResult: ...


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


def latest_timestamp(settings: Settings, dataset: str, zone_key: str) -> datetime | None:
    """Newest silver timestamp of a zone, or None when the zone has no silver yet."""
    zone_dir = settings.layer_dir("silver", dataset) / f"zone={zone_key}"
    if not zone_dir.exists():
        return None
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    pattern = (zone_dir / "**" / "*.parquet").as_posix()
    row = con.execute(
        f"SELECT max(timestamp_utc) FROM read_parquet('{pattern}', hive_partitioning=false)"
    ).fetchone()
    value = row[0] if row else None
    return value.astimezone(UTC) if value is not None else None


def plan_window(
    dataset: str,
    latest: datetime | None,
    now: datetime,
    *,
    lookback_days: int,
    initial_days: int,
) -> Window:
    """Request window: from the lookback start (UTC midnight) to the publication horizon."""
    if latest is not None:
        start_day = (latest - timedelta(days=lookback_days)).date()
    else:
        start_day = (now - timedelta(days=initial_days)).date()
    # Day-ahead prices for D+1 appear around 13:00 CET; ask until the end of D+1 (UTC).
    horizon_days = 2 if dataset == PRICES else 1
    end_day = now.date() + timedelta(days=horizon_days)
    return Window(
        datetime.combine(start_day, time.min, UTC), datetime.combine(end_day, time.min, UTC)
    )


def _is_active(zone: Zone, window: Window, now: datetime) -> bool:
    """Closed zones (valid_to in the past) and not yet valid zones are not pulled."""
    if zone.valid_to and zone.valid_to <= now.date():
        return False
    return not (zone.valid_from and zone.valid_from >= window.end.date())


def chunk_window(window: Window, max_days: int = 300) -> list[Window]:
    """ENTSO-E rejects windows over one year; split long catch-up windows."""
    chunks: list[Window] = []
    cursor = window.start
    while cursor < window.end:
        following = min(cursor + timedelta(days=max_days), window.end)
        chunks.append(Window(cursor, following))
        cursor = following
    return chunks


def _store_landing(
    settings: Settings, run_id: str, dataset: str, zone: Zone, window: Window, result: FetchResult
) -> Path:
    rel = (
        Path("live")
        / dataset
        / zone.key
        / f"{window.start:%Y%m%d}_{window.end:%Y%m%d}_{result.sha256[:12]}.xml"
    )
    path = settings.layer_dir("landing") / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.content)
    entry = {
        "run_id": run_id,
        "dataset": dataset,
        "zone_key": zone.key,
        "relpath": rel.as_posix(),
        "sha256": result.sha256,
        "bytes": len(result.content),
        "params": result.params,
        "retrieved_at_utc": result.retrieved_at.isoformat(),
    }
    with (settings.layer_dir("landing") / LIVE_MANIFEST).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return path


def _with_lineage(
    table: pa.Table, sha256: str, run_id: str, ingested_at: datetime
) -> pa.Table:
    n = table.num_rows
    table = table.append_column("source_sha256", pa.array([sha256] * n, pa.string()))
    table = table.append_column("run_id", pa.array([run_id] * n, pa.string()))
    return table.append_column(
        "ingested_at_utc", pa.array([ingested_at] * n, pa.timestamp("us", tz="UTC"))
    )


def _pull_chunk(
    settings: Settings,
    fetcher: Fetcher,
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    dataset: str,
    zone: Zone,
    window: Window,
    now: datetime,
    on_progress: Callable[[str], None] | None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dataset": dataset,
        "zone": zone.key,
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "rows": 0,
    }
    try:
        result = fetcher.fetch(dataset, zone, window.start, window.end)
    except EntsoeApiError as error:
        # One failing zone must not stop the others; the run reports it at the end.
        entry = {**base, "status": "error", "error": str(error)}
        if on_progress:
            on_progress(f"{dataset}/{zone.key}: ERROR {error}")
        return entry
    entry = {**base, "status": result.status, "sha256": result.sha256}
    if result.status == "data":
        path = _store_landing(settings, run_id, dataset, zone, window, result)
        rel = path.relative_to(settings.layer_dir("landing")).as_posix()
        table, _ = parse_document(path, dataset, zone.key, rel)
        if table.num_rows:
            table = _with_lineage(table, result.sha256, run_id, now)
            merged = merge_into_bronze(settings, dataset, zone.key, table)
            months = {(y, m) for y, m in merged["months"]}
            silver = build_silver_zone(con, settings, run_id, dataset, zone, months)
            entry.update(rows=merged["rows"], silver_rows=silver["silver_rows"])
    if on_progress:
        on_progress(f"{dataset}/{zone.key}: {entry['status']} rows={entry['rows']}")
    return entry



def pull_live(
    settings: Settings,
    fetcher: Fetcher,
    *,
    run_id: str | None = None,
    datasets: list[str] | None = None,
    zones: list[str] | None = None,
    lookback_days: int = 3,
    initial_days: int = 7,
    now: datetime | None = None,
    registry_path: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
    retry_pause: float = 60.0,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    registry = load_registry(registry_path)
    selected = [registry[k] for k in (zones or sorted(registry))]
    con = _connect(settings.threads)
    results: list[dict[str, Any]] = []
    work: list[tuple[str, Zone, Window]] = []
    for dataset in datasets or list(DATASETS):
        for zone in selected:
            window = plan_window(
                dataset,
                latest_timestamp(settings, dataset, zone.key),
                now,
                lookback_days=lookback_days,
                initial_days=initial_days,
            )
            if not _is_active(zone, window, now):
                continue
            for chunk in chunk_window(window):
                work.append((dataset, zone, chunk))
                entry = _pull_chunk(
                    settings, fetcher, con, run_id, dataset, zone, chunk, now, on_progress
                )
                results.append(entry)
    # The API has short outages. One more pass over the failures, after a pause, usually
    # clears them; whatever still fails is reported as an error.
    failed = [i for i, r in enumerate(results) if r["status"] == "error"]
    if failed:
        if on_progress:
            on_progress(f"retrying {len(failed)} failed pull(s) after {retry_pause:.0f}s")
        sleep(retry_pause)
        for i in failed:
            dataset, zone, chunk = work[i]
            results[i] = _pull_chunk(
                settings, fetcher, con, run_id, dataset, zone, chunk, now, on_progress
            )
    summary = {
        "lookback_days": lookback_days,
        "pulls": results,
        "rows": sum(r["rows"] for r in results),
        "no_data": sum(r["status"] == "no_data" for r in results),
        "errors": sum(r["status"] == "error" for r in results),
    }
    write_run_manifest(settings, run_id, "live", summary)
    return summary

