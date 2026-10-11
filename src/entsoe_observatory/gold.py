"""Gold layer: analysis-ready tables with versioned contracts, built in DuckDB.

Every table has a JSON contract under `contracts/gold/`. The contract drives the final
projection (column names, order and types) and is enforced on the written files before the
table directory is swapped in: schema, nullability, grain uniqueness and row constraints.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .bronze import new_run_id, write_run_manifest
from .parser import LOAD, PRICES
from .settings import Settings
from .storage import swap_directory
from .zones import load_registry

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "gold"
NEIGHBOURS = Path(__file__).resolve().parents[2] / "configs" / "neighbours.json"
MAX_FX_AGE_DAYS = 5  # ECB publishes working days only; weekends and holidays reuse the last rate
PEAK_FIRST_HOUR, PEAK_LAST_HOUR = 8, 19  # 08:00-20:00 local on weekdays

TABLE_ORDER = [
    "price_native",
    "load_native",
    "price_hourly",
    "load_hourly",
    "zone_daily",
    "cross_zone_spread_hourly",
    "weather_hourly",
    "dq_scorecard",
    "dq_scorecard_summary",
]


class ContractViolation(RuntimeError):
    def __init__(self, table: str, problems: list[str]) -> None:
        self.table = table
        self.problems = problems
        super().__init__(f"gold.{table} violates its contract: " + "; ".join(problems))


@dataclass(frozen=True)
class Contract:
    name: str
    version: str
    description: str
    grain: list[str]
    partition_by: list[str]
    columns: list[dict[str, Any]]
    constraints: list[str]

    @property
    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]


def load_contract(name: str, directory: Path | None = None) -> Contract:
    raw = json.loads(((directory or CONTRACT_DIR) / f"{name}.json").read_text(encoding="utf-8"))
    return Contract(
        name=raw["name"],
        version=raw["version"],
        description=raw["description"],
        grain=raw["grain"],
        partition_by=raw["partition_by"],
        columns=raw["columns"],
        constraints=raw.get("constraints", []),
    )


def project(contract: Contract, inner_sql: str) -> str:
    """Select exactly the contract columns, in order, cast to the declared types."""
    cols = ", ".join(f"CAST({c['name']} AS {c['type']}) AS {c['name']}" for c in contract.columns)
    order = ", ".join(contract.grain)
    return f"SELECT {cols} FROM ({inner_sql}) ORDER BY {order}"


def validate_files(
    con: duckdb.DuckDBPyConnection, contract: Contract, pattern: str
) -> list[str]:
    """Check written parquet files against the contract. Returns the list of problems."""
    source = f"read_parquet('{pattern}', hive_partitioning=false)"
    problems: list[str] = []
    described = con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    actual = [(row[0], row[1]) for row in described]
    expected = [(c["name"], c["type"]) for c in contract.columns]
    if actual != expected:
        problems.append(f"schema differs: expected {expected}, found {actual}")
        return problems
    for column in contract.columns:
        if not column["nullable"]:
            row = con.execute(
                f"SELECT count(*) FROM {source} WHERE {column['name']} IS NULL"
            ).fetchone()
            if row and row[0]:
                problems.append(f"{row[0]} nulls in non-nullable column {column['name']}")
    grain = ", ".join(contract.grain)
    row = con.execute(
        f"SELECT count(*) - count(DISTINCT ({grain})) FROM {source}"
    ).fetchone()
    if row and row[0]:
        problems.append(f"{row[0]} duplicate rows on grain ({grain})")
    for rule in contract.constraints:
        row = con.execute(
            f"SELECT count(*) FILTER (WHERE NOT coalesce(({rule}), true)) FROM {source}"
        ).fetchone()
        if row and row[0]:
            problems.append(f"{row[0]} rows violate: {rule}")
    return problems


PRICE_NATIVE_SQL = f"""
WITH base AS (
    SELECT p.* FROM silver_prices p JOIN zones z USING (zone_key)
    WHERE p.is_valid AND p.sequence = z.sdac_sequence
),
conv AS (
    SELECT b.*, f.units_per_eur AS rate, f.rate_date AS rate_date
    FROM base b ASOF LEFT JOIN fx f ON b.currency = f.currency AND b.local_date >= f.rate_date
)
SELECT
    zone_key, timestamp_utc, timestamp_local, local_date, resolution, resolution_minutes,
    sequence, value AS price_local, currency,
    CASE WHEN currency <> 'EUR' AND local_date - rate_date <= {MAX_FX_AGE_DAYS}
         THEN rate END AS fx_units_per_eur,
    CASE WHEN currency <> 'EUR' AND local_date - rate_date <= {MAX_FX_AGE_DAYS}
         THEN rate_date END AS fx_rate_date,
    CASE WHEN currency = 'EUR' THEN value
         WHEN local_date - rate_date <= {MAX_FX_AGE_DAYS} THEN value / rate END AS price_eur,
    CASE WHEN currency = 'EUR' THEN 'native'
         WHEN local_date - rate_date <= {MAX_FX_AGE_DAYS} THEN 'converted'
         ELSE 'missing_rate' END AS fx_status,
    is_a03_filled, revision_number, year(timestamp_utc) AS year
FROM conv
"""

LOAD_NATIVE_SQL = """
SELECT zone_key, timestamp_utc, timestamp_local, local_date, resolution, resolution_minutes,
       value AS load_mw, is_a03_filled, revision_number, year(timestamp_utc) AS year
FROM silver_load WHERE is_valid
"""

PRICE_HOURLY_SQL = """
WITH b AS (SELECT *, date_trunc('hour', timestamp_utc) AS hour_utc FROM g_price_native),
finest AS (SELECT zone_key, hour_utc, min(resolution_minutes) AS res FROM b GROUP BY 1, 2)
SELECT
    b.zone_key, b.hour_utc,
    timezone(z.tz, b.hour_utc) AS hour_local,
    CAST(timezone(z.tz, b.hour_utc) AS DATE) AS local_date,
    avg(b.price_local) AS price_local_mean,
    min(b.currency) AS currency,
    avg(b.price_eur) AS price_eur_mean,
    'PT' || CAST(f.res AS VARCHAR) || 'M' AS source_resolution,
    count(*) AS slots_present,
    60 // f.res AS slots_expected,
    bool_or(b.is_a03_filled) AS has_a03_filled,
    year(b.hour_utc) AS year
FROM b
JOIN finest f ON b.zone_key = f.zone_key AND b.hour_utc = f.hour_utc
    AND b.resolution_minutes = f.res
JOIN zones z ON z.zone_key = b.zone_key
GROUP BY b.zone_key, b.hour_utc, z.tz, f.res
"""

LOAD_HOURLY_SQL = """
WITH b AS (SELECT *, date_trunc('hour', timestamp_utc) AS hour_utc FROM g_load_native),
finest AS (SELECT zone_key, hour_utc, min(resolution_minutes) AS res FROM b GROUP BY 1, 2)
SELECT
    b.zone_key, b.hour_utc,
    timezone(z.tz, b.hour_utc) AS hour_local,
    CAST(timezone(z.tz, b.hour_utc) AS DATE) AS local_date,
    avg(b.load_mw) AS load_mw_mean,
    'PT' || CAST(f.res AS VARCHAR) || 'M' AS source_resolution,
    count(*) AS slots_present,
    60 // f.res AS slots_expected,
    bool_or(b.is_a03_filled) AS has_a03_filled,
    year(b.hour_utc) AS year
FROM b
JOIN finest f ON b.zone_key = f.zone_key AND b.hour_utc = f.hour_utc
    AND b.resolution_minutes = f.res
JOIN zones z ON z.zone_key = b.zone_key
GROUP BY b.zone_key, b.hour_utc, z.tz, f.res
"""

_PEAK = (
    "isodow(hour_local) <= 5 "
    f"AND hour(hour_local) BETWEEN {PEAK_FIRST_HOUR} AND {PEAK_LAST_HOUR}"
)
ZONE_DAILY_SQL = f"""
WITH p AS (
    SELECT zone_key, local_date,
           min(price_eur_mean) AS price_eur_min, max(price_eur_mean) AS price_eur_max,
           avg(price_eur_mean) AS price_eur_mean,
           avg(price_eur_mean) FILTER (WHERE {_PEAK}) AS price_eur_peak_mean,
           avg(price_eur_mean) FILTER (WHERE NOT ({_PEAK})) AS price_eur_offpeak_mean,
           count(*) FILTER (WHERE price_eur_mean < 0) AS negative_price_hours,
           sum(least(1.0, slots_present::DOUBLE / slots_expected)) AS price_hours
    FROM g_price_hourly GROUP BY 1, 2
),
l AS (
    SELECT zone_key, local_date, min(load_mw_mean) AS load_mw_min,
           max(load_mw_mean) AS load_mw_max, avg(load_mw_mean) AS load_mw_mean,
           sum(least(1.0, slots_present::DOUBLE / slots_expected)) AS load_hours
    FROM g_load_hourly GROUP BY 1, 2
),
keys AS (
    SELECT zone_key, local_date FROM p UNION SELECT zone_key, local_date FROM l
)
SELECT k.zone_key, k.local_date,
       CAST(date_diff('minute', timezone(z.tz, CAST(k.local_date AS TIMESTAMP)),
                      timezone(z.tz, CAST(k.local_date + 1 AS TIMESTAMP))) / 60 AS INTEGER)
           AS hours_in_day,
       p.price_eur_min, p.price_eur_max, p.price_eur_mean, p.price_eur_peak_mean,
       p.price_eur_offpeak_mean, p.negative_price_hours,
       p.price_hours / (date_diff('minute', timezone(z.tz, CAST(k.local_date AS TIMESTAMP)),
                        timezone(z.tz, CAST(k.local_date + 1 AS TIMESTAMP))) / 60.0)
           AS price_coverage,
       l.load_mw_min, l.load_mw_max, l.load_mw_mean,
       l.load_hours / (date_diff('minute', timezone(z.tz, CAST(k.local_date AS TIMESTAMP)),
                        timezone(z.tz, CAST(k.local_date + 1 AS TIMESTAMP))) / 60.0)
           AS load_coverage
FROM keys k
JOIN zones z ON z.zone_key = k.zone_key
LEFT JOIN p ON p.zone_key = k.zone_key AND p.local_date = k.local_date
LEFT JOIN l ON l.zone_key = k.zone_key AND l.local_date = k.local_date
"""

SPREAD_SQL = """
SELECT n.zone_a, n.zone_b, a.hour_utc,
       a.price_eur_mean AS price_eur_a, b.price_eur_mean AS price_eur_b,
       a.price_eur_mean - b.price_eur_mean AS spread_eur,
       abs(a.price_eur_mean - b.price_eur_mean) AS abs_spread_eur,
       year(a.hour_utc) AS year
FROM neighbours n
JOIN g_price_hourly a ON a.zone_key = n.zone_a
JOIN g_price_hourly b ON b.zone_key = n.zone_b AND b.hour_utc = a.hour_utc
WHERE a.price_eur_mean IS NOT NULL AND b.price_eur_mean IS NOT NULL
  AND a.slots_present = a.slots_expected AND b.slots_present = b.slots_expected
"""

WEATHER_SQL = """
SELECT w.zone_key, w.timestamp_utc AS hour_utc,
       CAST(timezone(z.tz, w.timestamp_utc) AS DATE) AS local_date,
       w.temperature_2m, w.wind_speed_10m, w.relative_humidity_2m, w.shortwave_radiation,
       w.points_used, w.points_total, w.source, year(w.timestamp_utc) AS year
FROM silver_weather w JOIN zones z USING (zone_key)
"""

SCORECARD_SQL = """
WITH s AS (
    SELECT dataset, zone_key, sequence, local_date, count(*) AS rows_total,
           count(*) FILTER (WHERE NOT is_valid) AS rows_invalid
    FROM silver_all GROUP BY ALL
)
SELECT c.dataset, c.zone_key, c.sequence, c.is_primary, c.local_date, c.expected_minutes,
       c.covered_minutes, c.coverage_ratio, c.status AS completeness_status,
       coalesce(s.rows_total, 0) AS rows_total, coalesce(s.rows_invalid, 0) AS rows_invalid,
       CASE WHEN s.rows_total > 0 THEN 1.0 - s.rows_invalid::DOUBLE / s.rows_total END
           AS validity_ratio,
       '{run_id}' AS dq_run_id
FROM dq_completeness c
LEFT JOIN s USING (dataset, zone_key, sequence, local_date)
"""

SUMMARY_SQL = """
WITH d AS (
    SELECT dataset, zone_key, count(*) AS days_total,
           count(*) FILTER (WHERE completeness_status = 'complete') AS days_complete,
           min(local_date) AS first_date, max(local_date) AS last_date
    FROM g_dq_scorecard WHERE is_primary GROUP BY ALL
),
f AS (
    SELECT dataset, zone_key,
           count(*) FILTER (WHERE severity = 'warning' AND status = 'fail') AS w,
           count(*) FILTER (WHERE severity = 'blocking' AND status = 'fail') AS b,
           max(checked_at_utc) AS checked_at_utc
    FROM dq_checks GROUP BY ALL
),
fr AS (
    SELECT dataset, zone_key, status = 'pass' AS ok, detail
    FROM dq_checks WHERE check_name = 'freshness'
)
SELECT d.dataset, d.zone_key, d.days_total, d.days_complete,
       d.days_complete::DOUBLE / d.days_total AS completeness_share,
       d.first_date, d.last_date,
       coalesce(f.w, 0) AS warning_checks_failed, coalesce(f.b, 0) AS blocking_checks_failed,
       fr.ok AS freshness_ok, fr.detail AS freshness_detail,
       '{run_id}' AS dq_run_id, f.checked_at_utc
FROM d
LEFT JOIN f USING (dataset, zone_key)
LEFT JOIN fr USING (dataset, zone_key)
"""

INNER_SQL = {
    "price_native": PRICE_NATIVE_SQL,
    "load_native": LOAD_NATIVE_SQL,
    "price_hourly": PRICE_HOURLY_SQL,
    "load_hourly": LOAD_HOURLY_SQL,
    "zone_daily": ZONE_DAILY_SQL,
    "cross_zone_spread_hourly": SPREAD_SQL,
    "weather_hourly": WEATHER_SQL,
    "dq_scorecard": SCORECARD_SQL,
    "dq_scorecard_summary": SUMMARY_SQL,
}


def _glob(path: Path) -> str:
    return (path / "**" / "*.parquet").as_posix()


def _setup(
    con: duckdb.DuckDBPyConnection, settings: Settings, registry_path: Path | None
) -> dict[str, str]:
    """Register zones, neighbours and the silver, FX and DQ sources. Returns missing inputs."""
    missing: dict[str, str] = {}
    con.execute(
        "CREATE OR REPLACE TEMP TABLE zones (zone_key VARCHAR, tz VARCHAR, sdac_sequence INTEGER)"
    )
    for zone in load_registry(registry_path).values():
        con.execute(
            "INSERT INTO zones VALUES (?, ?, ?)", [zone.key, zone.timezone, zone.sdac_sequence]
        )
    con.execute("CREATE OR REPLACE TEMP TABLE neighbours (zone_a VARCHAR, zone_b VARCHAR)")
    for a, b in json.loads(NEIGHBOURS.read_text(encoding="utf-8"))["pairs"]:
        con.execute("INSERT INTO neighbours VALUES (?, ?)", [a, b])

    def view(name: str, directory: Path, single_file: bool = False) -> None:
        if not directory.exists():
            missing[name] = str(directory)
            return
        target = directory.as_posix() if single_file else _glob(directory)
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW {name} AS "
            f"SELECT * FROM read_parquet('{target}', hive_partitioning=false)"
        )

    view("silver_prices", settings.layer_dir("silver", PRICES))
    view("silver_load", settings.layer_dir("silver", LOAD))
    view("silver_weather", settings.layer_dir("silver", "weather"))
    fx_file = settings.layer_dir("silver", "fx_daily") / "fx_daily.parquet"
    if fx_file.exists():
        con.execute(
            "CREATE OR REPLACE TEMP VIEW fx AS SELECT currency, rate_date, units_per_eur "
            f"FROM read_parquet('{fx_file.as_posix()}') WHERE is_valid"
        )
    else:
        missing["fx"] = str(fx_file)
    if "silver_prices" not in missing and "silver_load" not in missing:
        con.execute(
            "CREATE OR REPLACE TEMP VIEW silver_all AS "
            "SELECT dataset, zone_key, sequence, local_date, is_valid FROM silver_prices "
            "UNION ALL SELECT dataset, zone_key, sequence, local_date, is_valid FROM silver_load"
        )
    return missing


def _dq_run(settings: Settings) -> tuple[str, Path] | None:
    latest = settings.layer_dir("dq") / "latest.json"
    if not latest.exists():
        return None
    run_id = json.loads(latest.read_text(encoding="utf-8"))["run_id"]
    return run_id, settings.layer_dir("dq") / f"run_id={run_id}"


def _build_table(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    contract: Contract,
    inner_sql: str,
) -> dict[str, Any]:
    final_dir = settings.layer_dir("gold", contract.name)
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)
    query = project(contract, inner_sql)
    if contract.partition_by:
        parts = ", ".join(contract.partition_by)
        con.execute(
            f"COPY ({query}) TO '{tmp_dir.as_posix()}' (FORMAT parquet, COMPRESSION zstd, "
            f"PARTITION_BY ({parts}), WRITE_PARTITION_COLUMNS true, OVERWRITE_OR_IGNORE)"
        )
    else:
        tmp_dir.mkdir()
        con.execute(
            f"COPY ({query}) TO '{(tmp_dir / 'part-0.parquet').as_posix()}' "
            "(FORMAT parquet, COMPRESSION zstd)"
        )
    problems = validate_files(con, contract, _glob(tmp_dir))
    if problems:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ContractViolation(contract.name, problems)
    row = con.execute(
        f"SELECT count(*) FROM read_parquet('{_glob(tmp_dir)}', hive_partitioning=false)"
    ).fetchone()
    size = sum(f.stat().st_size for f in tmp_dir.rglob("*.parquet"))
    swap_directory(tmp_dir, final_dir)
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW g_{contract.name} AS "
        f"SELECT * FROM read_parquet('{_glob(final_dir)}', hive_partitioning=false)"
    )
    return {
        "table": contract.name,
        "contract_version": contract.version,
        "rows": row[0] if row else 0,
        "bytes": size,
    }


def build_gold(
    settings: Settings,
    *,
    run_id: str | None = None,
    tables: list[str] | None = None,
    registry_path: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    con = duckdb.connect()
    con.execute(f"SET threads={settings.threads}")
    con.execute("SET TimeZone='UTC'")
    missing = _setup(con, settings, registry_path)
    dq = _dq_run(settings)
    if dq is not None:
        dq_id, dq_dir = dq
        con.execute(
            "CREATE OR REPLACE TEMP VIEW dq_completeness AS "
            f"SELECT * FROM read_parquet('{(dq_dir / 'completeness_daily.parquet').as_posix()}')"
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW dq_checks AS "
            f"SELECT * FROM read_parquet('{(dq_dir / 'check_results.parquet').as_posix()}')"
        )
    for existing in TABLE_ORDER:  # lets a subset build reuse tables from earlier runs
        directory = settings.layer_dir("gold", existing)
        if directory.exists():
            con.execute(
                f"CREATE OR REPLACE TEMP VIEW g_{existing} AS "
                f"SELECT * FROM read_parquet('{_glob(directory)}', hive_partitioning=false)"
            )
    results: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    needs = {
        "price_native": ["silver_prices", "fx"],
        "load_native": ["silver_load"],
        "weather_hourly": ["silver_weather"],
        "dq_scorecard": ["silver_prices", "silver_load"],
    }
    # Dependencies that must already exist as gold views when a subset is requested.
    for name in tables or TABLE_ORDER:
        absent = [m for m in needs.get(name, []) if m in missing]
        if name.startswith("dq_") and dq is None:
            absent.append("dq run (run `dq` first)")
        if absent:
            skipped[name] = "missing input: " + ", ".join(absent)
            continue
        contract = load_contract(name)
        sql = INNER_SQL[name]
        if name in ("dq_scorecard", "dq_scorecard_summary") and dq is not None:
            sql = sql.replace("{run_id}", dq[0])
        entry = _build_table(con, settings, contract, sql)
        results.append(entry)
        if on_progress:
            on_progress(f"gold.{name}: {entry['rows']} rows, {entry['bytes'] / 2**20:.1f} MB")
    summary = {
        "tables": results,
        "skipped": skipped,
        "rows": sum(r["rows"] for r in results),
        "bytes": sum(r["bytes"] for r in results),
        "dq_run_id": dq[0] if dq else None,
    }
    write_run_manifest(settings, run_id, "gold", summary)
    return summary
