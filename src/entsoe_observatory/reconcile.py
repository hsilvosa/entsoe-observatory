"""Reconciliation of the new silver layer against the legacy parquet snapshot."""

from __future__ import annotations

from typing import Any

import duckdb

from .parser import LOAD, PRICES
from .settings import Settings


def reconcile(settings: Settings) -> list[dict[str, Any]]:
    """Per dataset: row counts, and legacy keys that are missing from silver (expected: 0)."""
    legacy_dir = settings.legacy_processed_dir
    if legacy_dir is None:
        raise ValueError("LEGACY_PROCESSED_DIR is not set")
    con = duckdb.connect()
    con.execute(f"SET threads={settings.threads}")
    con.execute("SET TimeZone='UTC'")
    report: list[dict[str, Any]] = []
    for dataset in (PRICES, LOAD):
        silver = (settings.layer_dir("silver", dataset) / "**" / "*.parquet").as_posix()
        legacy = (legacy_dir / dataset / "*.parquet").as_posix()
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW new_rows AS "
            f"SELECT zone_key, timestamp_utc, resolution, sequence "
            f"FROM read_parquet('{silver}', hive_partitioning=false)"
        )
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW old_rows AS "
            f"SELECT zone_key, timestamp_utc, resolution, "
            f"coalesce(classification_position, 0) AS sequence "
            f"FROM read_parquet('{legacy}')"
        )
        row = con.execute(
            """
            SELECT (SELECT count(*) FROM old_rows),
                   (SELECT count(*) FROM new_rows),
                   (SELECT count(*) FROM (SELECT DISTINCT * FROM old_rows) o
                    ANTI JOIN new_rows n USING (zone_key, timestamp_utc, resolution, sequence))
            """
        ).fetchone()
        assert row is not None
        report.append(
            {
                "dataset": dataset,
                "legacy_rows": row[0],
                "silver_rows": row[1],
                "added_rows": row[1] - row[0],
                "legacy_keys_missing_in_silver": row[2],
            }
        )
    return report
