"""Silver layer in DuckDB: typed, validated, deduplicated on the business key.

Business key: (dataset, zone_key, timestamp_utc, resolution, sequence). The newest revision
wins; a real point wins over an A03-filled one. The native resolution is never changed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import duckdb

from .bronze import write_run_manifest
from .parser import LOAD, PRICES
from .settings import Settings
from .storage import swap_directory
from .zones import Zone, load_registry

SILVER_SELECT = """
SELECT
    dataset,
    zone_key,
    timestamp_utc,
    timezone(tz, timestamp_utc) AS timestamp_local,
    CAST(timezone(tz, timestamp_utc) AS DATE) AS local_date,
    resolution,
    CASE resolution WHEN 'PT15M' THEN 15 WHEN 'PT30M' THEN 30 WHEN 'PT60M' THEN 60 END
        AS resolution_minutes,
    coalesce(sequence, 0) AS sequence,
    value,
    CASE WHEN dataset = 'day_ahead_prices' THEN currency || '/MWh' ELSE 'MW' END AS unit,
    currency,
    is_a03_filled,
    quality_flag,
    quality_flag = 'ok' AS is_valid,
    revision_number,
    document_id,
    source_file,
    run_id AS bronze_run_id,
    CAST(year(timestamp_utc) AS VARCHAR) AS year,
    lpad(CAST(month(timestamp_utc) AS VARCHAR), 2, '0') AS month
FROM ranked
WHERE rn = 1
"""

PREPARE_SQL = """
CREATE OR REPLACE TEMP TABLE ranked AS
SELECT *,
    row_number() OVER (
        PARTITION BY dataset, zone_key, timestamp_utc, resolution, coalesce(sequence, 0)
        ORDER BY revision_number DESC NULLS LAST, document_created_utc DESC NULLS LAST,
                 is_a03_filled ASC, source_file DESC
    ) AS rn
FROM (
    SELECT b.*, z.tz,
        CASE
            WHEN value IS NULL THEN 'null_value'
            WHEN dataset = 'actual_load' AND unit <> 'MAW' THEN 'unknown_unit'
            WHEN dataset = 'day_ahead_prices' AND (unit <> 'MWH' OR currency IS NULL)
                THEN 'unknown_unit'
            WHEN dataset = 'actual_load' AND value < 0 THEN 'negative_load'
            ELSE 'ok'
        END AS quality_flag
    FROM bronze_zone b
    JOIN zone_tz z USING (zone_key)
    WHERE b.timestamp_utc >= coalesce(b.request_start_utc, b.timestamp_utc)
      AND b.timestamp_utc < coalesce(b.request_end_utc, b.timestamp_utc + INTERVAL 1 SECOND)
)
"""


def _connect(threads: int) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET threads={int(threads)}")
    con.execute("SET TimeZone='UTC'")
    return con


def build_silver_zone(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    run_id: str,
    dataset: str,
    zone: Zone,
) -> dict[str, Any]:
    bronze_dir = settings.layer_dir("bronze", dataset) / f"zone={zone.key}"
    pattern = (bronze_dir / "**" / "*.parquet").as_posix()
    con.execute(
        "CREATE OR REPLACE TEMP TABLE bronze_zone AS "
        f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=false)"
    )
    con.execute("CREATE OR REPLACE TEMP TABLE zone_tz AS SELECT ? AS zone_key, ? AS tz",
                [zone.key, zone.timezone])
    bronze_rows = con.execute("SELECT count(*) FROM bronze_zone").fetchone()
    assert bronze_rows is not None
    con.execute(PREPARE_SQL)
    stats = con.execute(
        """
        SELECT count(*) FILTER (WHERE rn = 1),
               count(*) FILTER (WHERE rn > 1),
               count(*) FILTER (WHERE rn = 1 AND quality_flag <> 'ok'),
               count(*) FILTER (WHERE rn = 1 AND is_a03_filled)
        FROM ranked
        """
    ).fetchone()
    assert stats is not None
    silver_rows, duplicates, invalid, filled = stats
    final_dir = settings.layer_dir("silver", dataset) / f"zone={zone.key}"
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({SILVER_SELECT} ORDER BY timestamp_utc, resolution, sequence) "
        f"TO '{tmp_dir.as_posix()}' (FORMAT parquet, COMPRESSION zstd, "
        "PARTITION_BY (year, month), OVERWRITE_OR_IGNORE)"
    )
    swap_directory(tmp_dir, final_dir)
    return {
        "dataset": dataset,
        "zone": zone.key,
        "bronze_rows": bronze_rows[0],
        "out_of_window_dropped": bronze_rows[0] - silver_rows - duplicates,
        "duplicates_dropped": duplicates,
        "silver_rows": silver_rows,
        "invalid_rows": invalid,
        "a03_filled_rows": filled,
    }


def build_silver(
    settings: Settings,
    run_id: str,
    *,
    zones: list[str] | None = None,
    datasets: list[str] | None = None,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    con = _connect(settings.threads)
    results: list[dict[str, Any]] = []
    for dataset in datasets or [PRICES, LOAD]:
        base = settings.layer_dir("bronze", dataset)
        if not base.exists():
            continue
        for zone_dir in sorted(base.glob("zone=*")):
            key = zone_dir.name.split("=", 1)[1]
            if zone_dir.suffix == ".tmp" or (zones and key not in zones):
                continue
            results.append(build_silver_zone(con, settings, run_id, dataset, registry[key]))
    summary = {
        "partitions": results,
        "bronze_rows": sum(r["bronze_rows"] for r in results),
        "out_of_window_dropped": sum(r["out_of_window_dropped"] for r in results),
        "duplicates_dropped": sum(r["duplicates_dropped"] for r in results),
        "silver_rows": sum(r["silver_rows"] for r in results),
        "invalid_rows": sum(r["invalid_rows"] for r in results),
        "a03_filled_rows": sum(r["a03_filled_rows"] for r in results),
    }
    write_run_manifest(settings, run_id, "silver", summary)
    return {k: v for k, v in summary.items() if k != "partitions"}
