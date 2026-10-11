"""Data quality checks in SQL over the silver layer.

Every check returns rows in one schema and the results are stored under `dq/`:

- `dq/run_id=<id>/check_results.parquet`: one row per (check, dataset, zone).
- `dq/run_id=<id>/completeness_daily.parquet`: coverage of each local day per series.
- `dq/latest.json`: summary of the newest run.

Blocking checks fail the run (`DataQualityError`); the others are warnings or information.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from .bronze import new_run_id
from .parser import LOAD, PRICES
from .settings import Settings
from .zones import load_registry

COMPLETE_MIN = 0.999
COMPLETE_MAX = 1.001
EUR_PRICE_RANGE = (-500.0, 4000.0)  # SDAC harmonised min/max clearing price
MAX_LOAD_MW = 150_000.0
LOAD_LAG_HOURS = 3.0  # actual load is published within about 2 h
PRICE_PUBLICATION_HOUR_CET = 14  # day-ahead prices for D+1 are out by 14:00 CET
MARKET_TZ = "Europe/Brussels"

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"


class DataQualityError(RuntimeError):
    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = failures
        names = sorted({f"{f['check_name']}:{f['dataset']}:{f['zone_key']}" for f in failures})
        super().__init__(f"{len(failures)} blocking data quality failures: {', '.join(names[:10])}")


@dataclass(frozen=True)
class DqReport:
    run_id: str
    path: Path
    blocking_failures: list[dict[str, Any]]
    warning_failures: int
    checks_run: int


DAY_MINUTES = (
    "date_diff('minute', timezone(tz, CAST(local_date AS TIMESTAMP)),"
    " timezone(tz, CAST(local_date + 1 AS TIMESTAMP)))"
)

COMPLETENESS_SQL = f"""
CREATE OR REPLACE TEMP TABLE completeness_daily AS
WITH covered AS (
    SELECT zone_key, sequence, local_date,
           sum(resolution_minutes) AS covered_minutes,
           count(*) AS slots_present,
           string_agg(DISTINCT resolution, ',' ORDER BY resolution) AS resolution_list
    FROM s WHERE is_valid GROUP BY ALL
),
span AS (
    SELECT zone_key, sequence, min(local_date) AS d0, max(local_date) AS d1 FROM s GROUP BY ALL
),
days AS (
    SELECT zone_key, sequence, d0, d1,
           CAST(unnest(generate_series(d0, d1, INTERVAL 1 DAY)) AS DATE) AS local_date
    FROM span
),
joined AS (
    SELECT d.zone_key, d.sequence, d.local_date, d.d0, d.d1, z.tz,
           coalesce(c.covered_minutes, 0) AS covered_minutes,
           coalesce(c.slots_present, 0) AS slots_present,
           c.resolution_list
    FROM days d
    JOIN zones z USING (zone_key)
    LEFT JOIN covered c USING (zone_key, sequence, local_date)
),
measured AS (
    SELECT *, {DAY_MINUTES} AS expected_minutes FROM joined
)
SELECT '{{dataset}}' AS dataset, zone_key, sequence, local_date, resolution_list, slots_present,
       covered_minutes, expected_minutes,
       covered_minutes / expected_minutes AS coverage_ratio,
       CASE
           WHEN covered_minutes / expected_minutes < {COMPLETE_MIN} THEN 'incomplete'
           WHEN covered_minutes / expected_minutes > {COMPLETE_MAX} THEN 'overlap'
           ELSE 'complete'
       END AS status
FROM measured
-- The first and last local day of a series are partial by construction (the request window
-- starts or ends mid-day for some zones); they are not reported as gaps.
WHERE NOT (local_date IN (d0, d1) AND covered_minutes < expected_minutes * {COMPLETE_MIN})
"""

CHECKS_SQL = """
CREATE OR REPLACE TEMP TABLE results AS
-- completeness (warning): days whose covered minutes differ from the DST-aware day length
SELECT 'completeness' AS check_name, '{warning}' AS severity, '{dataset}' AS dataset, zone_key,
       count(*) FILTER (WHERE status <> 'complete') AS failed_count, count(*) AS total_count,
       'worst day ' || CAST(arg_min(local_date, coverage_ratio) AS VARCHAR)
           || ' coverage ' || CAST(round(min(coverage_ratio), 3) AS VARCHAR) AS detail
FROM completeness_daily GROUP BY zone_key
UNION ALL
-- duplicates on the business key (blocking)
SELECT 'duplicate_business_key', '{blocking}', '{dataset}', zone_key,
       count(*) - count(DISTINCT (timestamp_utc, resolution, sequence)), count(*), ''
FROM s GROUP BY zone_key
UNION ALL
-- unit and currency consistency (blocking)
SELECT 'unit_consistency', '{blocking}', '{dataset}', zone_key,
       count(*) FILTER (WHERE quality_flag = 'unknown_unit'), count(*), ''
FROM s GROUP BY zone_key
UNION ALL
SELECT 'currency_consistency', '{blocking}', '{dataset}', s.zone_key,
       count(*) FILTER (WHERE '{dataset}' = 'day_ahead_prices'
                        AND NOT list_contains(z.currencies, s.currency)),
       count(*),
       coalesce(string_agg(DISTINCT s.currency, ',' ORDER BY s.currency), '')
FROM s JOIN zones z USING (zone_key) GROUP BY s.zone_key
UNION ALL
-- invalid values flagged in silver (warning): negative load, null values
SELECT 'invalid_values', '{warning}', '{dataset}', zone_key,
       count(*) FILTER (WHERE NOT is_valid), count(*),
       coalesce(string_agg(DISTINCT quality_flag, ',') FILTER (WHERE NOT is_valid), '')
FROM s GROUP BY zone_key
UNION ALL
-- value ranges (warning)
SELECT 'value_range', '{warning}', '{dataset}', zone_key,
       count(*) FILTER (WHERE {range_predicate}), count(*), ''
FROM s GROUP BY zone_key
UNION ALL
-- resolution changes (info): count of transitions and the first ones
SELECT 'resolution_changes', '{info}', '{dataset}', zone_key,
       count(*), count(*), string_agg(change, '; ' ORDER BY d)
FROM (
    SELECT zone_key, sequence, local_date AS d,
           previous || ' -> ' || resolution_list || ' on ' || CAST(local_date AS VARCHAR)
               AS change
    FROM (
        SELECT zone_key, sequence, local_date, resolution_list,
               lag(resolution_list) OVER (PARTITION BY zone_key, sequence ORDER BY local_date)
                   AS previous
        FROM completeness_daily WHERE resolution_list IS NOT NULL
    ) WHERE previous IS NOT NULL AND previous <> resolution_list
) GROUP BY zone_key
UNION ALL
-- currency changes (info): transitions such as BGN -> EUR matter for EUR conversion
SELECT 'currency_changes', '{info}', '{dataset}', zone_key,
       count(*) - 1, count(*), string_agg(span, '; ' ORDER BY first_day)
FROM (
    SELECT zone_key, min(local_date) AS first_day,
           currency || ' ' || CAST(min(local_date) AS VARCHAR) || '..'
               || CAST(max(local_date) AS VARCHAR) AS span
    FROM s WHERE currency IS NOT NULL GROUP BY zone_key, currency
) GROUP BY zone_key
UNION ALL
-- freshness (warning)
{freshness_sql}
"""

FX_COVERAGE_SQL = """
CREATE OR REPLACE TEMP TABLE results AS
-- fx coverage (warning): non-EUR price days without an ECB rate in the 5 days before
SELECT 'fx_coverage' AS check_name, '{warning}' AS severity, 'day_ahead_prices' AS dataset,
       zone_key, count(*) FILTER (WHERE rates = 0) AS failed_count, count(*) AS total_count,
       coalesce(string_agg(DISTINCT currency, ',') FILTER (WHERE rates = 0), '') AS detail
FROM (
    SELECT d.zone_key, d.currency, d.local_date, count(f.rate_date) AS rates
    FROM (SELECT DISTINCT zone_key, currency, local_date FROM s WHERE currency <> 'EUR') d
    LEFT JOIN fx f ON f.currency = d.currency
        AND f.rate_date BETWEEN d.local_date - 5 AND d.local_date AND f.is_valid
    GROUP BY ALL
) GROUP BY zone_key
"""

PRICE_FRESHNESS = """
SELECT 'freshness', '{warning}', '{dataset}', f.zone_key,
       CASE WHEN f.last_date < {expected_date} THEN 1 ELSE 0 END, 1,
       'last local date ' || CAST(f.last_date AS VARCHAR)
       || ', expected at least ' || CAST({expected_date} AS VARCHAR)
FROM (SELECT zone_key, max(local_date) AS last_date FROM s GROUP BY zone_key) f
JOIN zones z USING (zone_key)
WHERE z.valid_to IS NULL OR z.valid_to > CAST('{now_utc}' AS DATE)
"""

LOAD_FRESHNESS = """
SELECT 'freshness', '{warning}', '{dataset}', f.zone_key,
       CASE WHEN date_diff('minute', f.last_end, CAST('{now_utc}+00' AS TIMESTAMPTZ))
                 > {lag_minutes} THEN 1 ELSE 0 END, 1,
       'lag hours ' || CAST(round(date_diff('minute', f.last_end,
            CAST('{now_utc}+00' AS TIMESTAMPTZ)) / 60.0, 1) AS VARCHAR)
FROM (
    SELECT zone_key, max(timestamp_utc + to_minutes(resolution_minutes)) AS last_end
    FROM s GROUP BY zone_key
) f
JOIN zones z USING (zone_key)
WHERE z.valid_to IS NULL OR z.valid_to > CAST('{now_utc}' AS DATE)
"""


def _range_predicate(dataset: str) -> str:
    if dataset == PRICES:
        low, high = EUR_PRICE_RANGE
        return f"currency = 'EUR' AND (value < {low} OR value > {high})"
    return f"value > {MAX_LOAD_MW}"


def _freshness_sql(dataset: str, now: datetime) -> str:
    now_utc = now.strftime("%Y-%m-%d %H:%M:%S")
    if dataset == PRICES:
        market_now = now.astimezone(ZoneInfo(MARKET_TZ))
        offset = 1 if market_now.hour >= PRICE_PUBLICATION_HOUR_CET else 0
        # One day of tolerance: prices for D+1 can slip past the publication hour.
        expected = f"(CAST('{market_now.date().isoformat()}' AS DATE) + {offset} - 1)"
        return PRICE_FRESHNESS.format(
            warning=WARNING, dataset=dataset, expected_date=expected, now_utc=now_utc
        )
    return LOAD_FRESHNESS.format(
        warning=WARNING, dataset=dataset, now_utc=now_utc, lag_minutes=int(LOAD_LAG_HOURS * 60)
    )


def run_checks(
    settings: Settings,
    *,
    run_id: str | None = None,
    datasets: list[str] | None = None,
    now: datetime | None = None,
    registry_path: Path | None = None,
    fail_on_blocking: bool = True,
) -> DqReport:
    run_id = run_id or new_run_id()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    registry = load_registry(registry_path)
    con = duckdb.connect()
    con.execute(f"SET threads={settings.threads}")
    con.execute("SET TimeZone='UTC'")
    con.execute(
        "CREATE TEMP TABLE zones (zone_key VARCHAR, tz VARCHAR, currencies VARCHAR[],"
        " valid_to DATE)"
    )
    for zone in registry.values():
        con.execute(
            "INSERT INTO zones VALUES (?, ?, ?, ?)",
            [zone.key, zone.timezone, zone.currencies, zone.valid_to],
        )
    out_dir = settings.layer_dir("dq") / f"run_id={run_id}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    con.execute(
        "CREATE TEMP TABLE all_results (run_id VARCHAR, checked_at_utc TIMESTAMPTZ,"
        " check_name VARCHAR, severity VARCHAR, dataset VARCHAR, zone_key VARCHAR,"
        " status VARCHAR, failed_count BIGINT, total_count BIGINT, detail VARCHAR)"
    )
    con.execute(
        "CREATE TEMP TABLE all_completeness (dataset VARCHAR, zone_key VARCHAR,"
        " sequence INTEGER, local_date DATE, resolution_list VARCHAR, slots_present BIGINT,"
        " covered_minutes BIGINT, expected_minutes BIGINT, coverage_ratio DOUBLE,"
        " status VARCHAR)"
    )
    for dataset in datasets or [PRICES, LOAD]:
        if not settings.layer_dir("silver", dataset).exists():
            continue
        pattern = (settings.layer_dir("silver", dataset) / "**" / "*.parquet").as_posix()
        con.execute(
            "CREATE OR REPLACE TEMP VIEW s AS "
            f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=false)"
        )
        con.execute(COMPLETENESS_SQL.format(dataset=dataset))
        con.execute("INSERT INTO all_completeness SELECT * FROM completeness_daily")
        con.execute(
            CHECKS_SQL.format(
                dataset=dataset,
                blocking=BLOCKING,
                warning=WARNING,
                info=INFO,
                range_predicate=_range_predicate(dataset),
                freshness_sql=_freshness_sql(dataset, now),
            )
        )
        insert = (
            "INSERT INTO all_results SELECT ?, CAST(? AS TIMESTAMPTZ), check_name, severity,"
            " dataset, zone_key, CASE WHEN severity = 'info' OR failed_count = 0 THEN 'pass'"
            " ELSE 'fail' END, failed_count, total_count, detail FROM results"
        )
        con.execute(insert, [run_id, now.isoformat()])
        fx_file = settings.layer_dir("silver", "fx_daily") / "fx_daily.parquet"
        if dataset == PRICES and fx_file.exists():
            con.execute(
                "CREATE OR REPLACE TEMP VIEW fx AS "
                f"SELECT * FROM read_parquet('{fx_file.as_posix()}')"
            )
            con.execute(FX_COVERAGE_SQL.format(warning=WARNING))
            con.execute(insert, [run_id, now.isoformat()])
    results_path = (out_dir / "check_results.parquet").as_posix()
    completeness_path = (out_dir / "completeness_daily.parquet").as_posix()
    con.execute(
        "COPY (SELECT * FROM all_results ORDER BY dataset, check_name, zone_key) "
        f"TO '{results_path}' (FORMAT parquet, COMPRESSION zstd)"
    )
    con.execute(
        "COPY (SELECT * FROM all_completeness ORDER BY dataset, zone_key, sequence, local_date)"
        f" TO '{completeness_path}' (FORMAT parquet, COMPRESSION zstd)"
    )
    cursor = con.execute(
        "SELECT check_name, severity, dataset, zone_key, failed_count, total_count, detail"
        " FROM all_results WHERE status = 'fail' ORDER BY 1, 3, 4"
    )
    names = [d[0] for d in cursor.description]
    failures = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    blocking = [f for f in failures if f["severity"] == BLOCKING]
    warnings = [f for f in failures if f["severity"] == WARNING]
    total = con.execute("SELECT count(*) FROM all_results").fetchone()
    assert total is not None
    latest = {
        "run_id": run_id,
        "checked_at_utc": now.isoformat(),
        "checks_run": total[0],
        "blocking_failures": len(blocking),
        "warning_failures": len(warnings),
        "blocking": blocking[:50],
    }
    (settings.layer_dir("dq") / "latest.json").write_text(
        json.dumps(latest, indent=1, default=str), encoding="utf-8"
    )
    report = DqReport(run_id, out_dir, blocking, len(warnings), total[0])
    if blocking and fail_on_blocking:
        raise DataQualityError(blocking)
    return report
