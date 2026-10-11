"""Landing manifest and bronze layer built from raw ENTSO-E XML."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .parser import DATASETS, parse_document
from .settings import Settings
from .storage import sha256_file, swap_directory

LANDING_MANIFEST = "landing_manifest.json"


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def code_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def write_run_manifest(settings: Settings, run_id: str, stage: str, body: dict[str, Any]) -> Path:
    directory = settings.layer_dir("manifests")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}_{stage}.json"
    payload = {
        "run_id": run_id,
        "stage": stage,
        "code_version": code_version(),
        "created_at_utc": datetime.now(UTC).isoformat(),
        **body,
    }
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return path


@dataclass(frozen=True)
class LandingEntry:
    dataset: str
    zone_key: str
    start: str
    end: str
    relpath: str
    bytes: int
    sha256: str


def build_landing_manifest(settings: Settings, run_id: str) -> list[LandingEntry]:
    """Checksum every raw XML file listed as `data` in the downloader manifest."""
    raw_dir = settings.require_raw_dir()
    download = json.loads((raw_dir / "download_manifest.json").read_text(encoding="utf-8"))
    entries: list[LandingEntry] = []
    for task in sorted(download["tasks"], key=lambda t: t["task_id"]):
        if task["status"] != "data" or task["dataset"] not in DATASETS:
            continue
        path = raw_dir / task["file"]
        entries.append(
            LandingEntry(
                dataset=task["dataset"],
                zone_key=task["zone"]["key"],
                start=task["start"],
                end=task["end"],
                relpath=task["file"],
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    target = settings.layer_dir("landing")
    target.mkdir(parents=True, exist_ok=True)
    (target / LANDING_MANIFEST).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "raw_dir_name": raw_dir.name,
                "files": [asdict(e) for e in entries],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return entries


def load_landing_manifest(settings: Settings) -> list[LandingEntry]:
    path = settings.layer_dir("landing") / LANDING_MANIFEST
    data = json.loads(path.read_text(encoding="utf-8"))
    return [LandingEntry(**item) for item in data["files"]]


def _partition_columns(table: pa.Table) -> pa.Table:
    ts = table["timestamp_utc"]
    year = pc.cast(pc.year(ts), pa.string())
    month = pc.utf8_lpad(pc.cast(pc.month(ts), pa.string()), 2, "0")
    return table.append_column("year", year).append_column("month", month)


def build_bronze_zone(
    settings: Settings,
    run_id: str,
    dataset: str,
    zone_key: str,
    entries: list[LandingEntry],
) -> dict[str, Any]:
    """Parse every file of one (dataset, zone) and swap the zone directory atomically."""
    raw_dir = settings.require_raw_dir()
    ingested_at = datetime.now(UTC)
    tables: list[pa.Table] = []
    filled = 0
    for entry in entries:
        table, stats = parse_document(raw_dir / entry.relpath, dataset, zone_key, entry.relpath)
        if table.num_rows == 0:
            continue
        n = table.num_rows
        table = table.append_column("source_sha256", pa.array([entry.sha256] * n, pa.string()))
        tables.append(table)
        filled += stats.filled
    final_dir = settings.layer_dir("bronze", dataset) / f"zone={zone_key}"
    if not tables:
        return {"dataset": dataset, "zone": zone_key, "files": len(entries), "rows": 0}
    combined = pa.concat_tables(tables)
    n = combined.num_rows
    combined = combined.append_column("run_id", pa.array([run_id] * n, pa.string()))
    combined = combined.append_column(
        "ingested_at_utc", pa.array([ingested_at] * n, pa.timestamp("us", tz="UTC"))
    )
    combined = _partition_columns(combined)
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    if tmp_dir.exists():
        import shutil

        shutil.rmtree(tmp_dir)
    pq.write_to_dataset(
        combined,
        root_path=str(tmp_dir),
        partition_cols=["year", "month"],
        compression="zstd",
        existing_data_behavior="overwrite_or_ignore",
    )
    swap_directory(tmp_dir, final_dir)
    return {
        "dataset": dataset,
        "zone": zone_key,
        "files": len(entries),
        "rows": n,
        "a03_filled_rows": filled,
    }


def build_bronze(
    settings: Settings,
    run_id: str,
    *,
    zones: list[str] | None = None,
    datasets: list[str] | None = None,
) -> dict[str, Any]:
    entries = load_landing_manifest(settings)
    groups: dict[tuple[str, str], list[LandingEntry]] = {}
    for entry in entries:
        if zones and entry.zone_key not in zones:
            continue
        if datasets and entry.dataset not in datasets:
            continue
        groups.setdefault((entry.dataset, entry.zone_key), []).append(entry)
    results = [
        build_bronze_zone(settings, run_id, dataset, zone, files)
        for (dataset, zone), files in sorted(groups.items())
    ]
    summary = {
        "inputs": [{"relpath": e.relpath, "sha256": e.sha256} for g in groups.values() for e in g],
        "partitions": results,
        "rows": sum(r["rows"] for r in results),
        "a03_filled_rows": sum(r.get("a03_filled_rows", 0) for r in results),
    }
    write_run_manifest(settings, run_id, "bronze", summary)
    return {k: v for k, v in summary.items() if k != "inputs"}


def merge_into_bronze(
    settings: Settings, dataset: str, zone_key: str, table: pa.Table
) -> dict[str, Any]:
    """Merge new rows into the affected month partitions (idempotent per payload checksum).

    Each affected month directory is rebuilt next to the old one and swapped. Rows from a
    payload that is already stored (same `source_sha256`) are replaced, never duplicated.
    """
    import shutil

    table = _partition_columns(table)
    zone_dir = settings.layer_dir("bronze", dataset) / f"zone={zone_key}"
    months = sorted(
        {(y, m) for y, m in zip(table["year"].to_pylist(), table["month"].to_pylist(), strict=True)}
    )
    new_checksums = set(table["source_sha256"].to_pylist())
    touched: list[tuple[str, str]] = []
    added = 0
    for year, month in months:
        mask = pc.and_(pc.equal(table["year"], year), pc.equal(table["month"], month))
        fresh = table.filter(mask).drop_columns(["year", "month"])
        month_dir = zone_dir / f"year={year}" / f"month={month}"
        parts = [fresh]
        if month_dir.exists():
            existing = pq.read_table(month_dir)
            keep = pc.invert(pc.is_in(existing["source_sha256"], pa.array(list(new_checksums))))
            parts.insert(0, existing.filter(keep).select(fresh.column_names).cast(fresh.schema))
        tmp_dir = month_dir.with_name(month_dir.name + ".tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)
        pq.write_table(pa.concat_tables(parts), tmp_dir / "part-0.parquet", compression="zstd")
        swap_directory(tmp_dir, month_dir)
        touched.append((year, month))
        added += fresh.num_rows
    return {"months": touched, "rows": added}
