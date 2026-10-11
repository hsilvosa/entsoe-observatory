"""Optional Spark implementation of the silver transformations.

It mirrors `silver.py` (same business key, ranking and validity rules) and is checked against
the DuckDB path by a parity test. Requires the `spark` extra and a working local Spark.
"""

from __future__ import annotations

from typing import Any

SILVER_SQL = """
WITH prepared AS (
    SELECT b.*,
        CASE
            WHEN value IS NULL THEN 'null_value'
            WHEN dataset = 'actual_load' AND unit <> 'MAW' THEN 'unknown_unit'
            WHEN dataset = 'day_ahead_prices' AND (unit <> 'MWH' OR currency IS NULL)
                THEN 'unknown_unit'
            WHEN dataset = 'actual_load' AND value < 0 THEN 'negative_load'
            ELSE 'ok'
        END AS quality_flag
    FROM bronze b
    WHERE timestamp_utc >= coalesce(request_start_utc, timestamp_utc)
      AND timestamp_utc < coalesce(request_end_utc, timestamp_utc + INTERVAL 1 SECOND)
),
ranked AS (
    SELECT *,
        row_number() OVER (
            PARTITION BY dataset, zone_key, timestamp_utc, resolution, coalesce(sequence, 0)
            ORDER BY revision_number DESC NULLS LAST, document_created_utc DESC NULLS LAST,
                     is_a03_filled ASC, source_file DESC
        ) AS rn
    FROM prepared
)
SELECT
    dataset,
    zone_key,
    timestamp_utc,
    from_utc_timestamp(timestamp_utc, '{tz}') AS timestamp_local,
    CAST(from_utc_timestamp(timestamp_utc, '{tz}') AS DATE) AS local_date,
    resolution,
    CASE resolution WHEN 'PT15M' THEN 15 WHEN 'PT30M' THEN 30 WHEN 'PT60M' THEN 60 END
        AS resolution_minutes,
    coalesce(sequence, 0) AS sequence,
    value,
    CASE WHEN dataset = 'day_ahead_prices' THEN concat(currency, '/MWh') ELSE 'MW' END AS unit,
    currency,
    is_a03_filled,
    quality_flag,
    quality_flag = 'ok' AS is_valid,
    revision_number,
    document_id,
    source_file
FROM ranked
WHERE rn = 1
"""


def silver_zone_spark(spark: Any, bronze_path: str, timezone: str) -> Any:
    """Return the silver DataFrame of one (dataset, zone) bronze directory."""
    spark.read.parquet(bronze_path).createOrReplaceTempView("bronze")
    return spark.sql(SILVER_SQL.format(tz=timezone))
