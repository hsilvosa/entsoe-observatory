"""Static dashboard: exports small aggregates from gold as JSON next to a vanilla HTML/JS app.

The output directory can be served by any static host (Cloudflare Pages). The app has no
external dependencies, so the site works offline and builds reproducibly.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from .settings import Settings
from .zones import load_registry

APP_DIR = Path(__file__).resolve().parents[2] / "dashboard"


def _round(values: list[Any], digits: int = 2) -> list[Any]:
    return [None if v is None else round(float(v), digits) for v in values]


def _write(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text)


def build_dashboard(
    settings: Settings, out_dir: Path, *, registry_path: Path | None = None
) -> dict[str, Any]:
    gold = settings.layer_dir("gold")
    if not (gold / "zone_daily").exists():
        raise FileNotFoundError("gold tables are missing: run `gold` first")
    registry = load_registry(registry_path)
    con = duckdb.connect()
    con.execute(f"SET threads={settings.threads}")
    con.execute("SET TimeZone='UTC'")
    for table in (
        "zone_daily", "cross_zone_spread_hourly", "dq_scorecard", "dq_scorecard_summary",
    ):
        if (gold / table).exists():
            glob = (gold / table / "**" / "*.parquet").as_posix()
            con.execute(
                f"CREATE VIEW {table} AS "
                f"SELECT * FROM read_parquet('{glob}', hive_partitioning=false)"
            )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for asset in APP_DIR.iterdir():
        if asset.is_dir():
            shutil.copytree(asset, out_dir / asset.name)
        else:
            shutil.copy(asset, out_dir / asset.name)
    data = out_dir / "data"
    sizes: dict[str, int] = {}

    # Coverage and scorecard summary
    cursor = con.execute(
        "SELECT dataset, zone_key, days_total, days_complete, completeness_share,"
        " CAST(first_date AS VARCHAR), CAST(last_date AS VARCHAR), warning_checks_failed,"
        " blocking_checks_failed, freshness_ok, freshness_detail, dq_run_id,"
        " CAST(checked_at_utc AS VARCHAR) FROM dq_scorecard_summary ORDER BY dataset, zone_key"
    )
    cols = [d[0] for d in cursor.description]
    summary = [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]
    dq_run = summary[0]["dq_run_id"] if summary else None
    meta = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dq_run_id": dq_run,
        "zones": [
            {"key": z.key, "name": z.name, "country": z.country_code, "timezone": z.timezone}
            for z in registry.values()
        ],
    }
    sizes["meta"] = _write(data / "meta.json", meta)
    sizes["coverage"] = _write(data / "coverage.json", summary)

    # Monthly completeness heat map (primary series only)
    rows = con.execute(
        "SELECT dataset, zone_key, strftime(local_date, '%Y-%m') AS month,"
        " avg(least(1.0, coverage_ratio)) FROM dq_scorecard WHERE is_primary"
        " GROUP BY ALL ORDER BY 1, 2, 3"
    ).fetchall()
    months = sorted({r[2] for r in rows})
    index = {m: i for i, m in enumerate(months)}
    heat: dict[str, dict[str, list[float | None]]] = {}
    for dataset, zone, month, value in rows:
        series = heat.setdefault(dataset, {}).setdefault(zone, [None] * len(months))
        series[index[month]] = round(float(value), 3)
    sizes["completeness_monthly"] = _write(
        data / "completeness_monthly.json", {"months": months, "series": heat}
    )

    # Per-zone daily series
    zone_rows = con.execute(
        "SELECT zone_key, CAST(local_date AS VARCHAR), price_eur_mean, price_eur_min,"
        " price_eur_max, negative_price_hours, load_mw_mean, price_coverage, load_coverage"
        " FROM zone_daily ORDER BY zone_key, local_date"
    ).fetchall()
    per_zone: dict[str, list[tuple[Any, ...]]] = {}
    for row in zone_rows:
        per_zone.setdefault(row[0], []).append(row[1:])
    for zone, items in per_zone.items():
        columns = list(zip(*items, strict=True))
        sizes[f"zone_{zone}"] = _write(
            data / "zones" / f"{zone}.json",
            {
                "zone": zone,
                "dates": list(columns[0]),
                "price_mean": _round(list(columns[1])),
                "price_min": _round(list(columns[2])),
                "price_max": _round(list(columns[3])),
                "negative_hours": list(columns[4]),
                "load_mean": _round(list(columns[5]), 0),
                "price_coverage": _round(list(columns[6]), 3),
                "load_coverage": _round(list(columns[7]), 3),
            },
        )

    # Negative price hours per zone and year
    neg = con.execute(
        "SELECT zone_key, year(local_date), sum(negative_price_hours)::INTEGER FROM zone_daily"
        " WHERE price_eur_mean IS NOT NULL GROUP BY ALL ORDER BY 1, 2"
    ).fetchall()
    years = sorted({r[1] for r in neg})
    negative: dict[str, list[int]] = {}
    for zone, year, hours in neg:
        negative.setdefault(zone, [0] * len(years))[years.index(year)] = hours or 0
    sizes["negative_hours"] = _write(
        data / "negative_hours.json", {"years": years, "zones": negative}
    )

    # Monthly spreads per neighbouring pair
    if (gold / "cross_zone_spread_hourly").exists():
        spread_rows = con.execute(
            "SELECT zone_a, zone_b, strftime(hour_utc, '%Y-%m'), avg(spread_eur),"
            " avg(abs_spread_eur) FROM cross_zone_spread_hourly GROUP BY ALL ORDER BY 1, 2, 3"
        ).fetchall()
        spread_months = sorted({r[2] for r in spread_rows})
        pos = {m: i for i, m in enumerate(spread_months)}
        pairs: dict[tuple[str, str], dict[str, list[float | None]]] = {}
        for a, b, month, mean, abs_mean in spread_rows:
            entry = pairs.setdefault(
                (a, b),
                {"mean": [None] * len(spread_months), "abs_mean": [None] * len(spread_months)},
            )
            entry["mean"][pos[month]] = round(float(mean), 2)
            entry["abs_mean"][pos[month]] = round(float(abs_mean), 2)
        sizes["spreads"] = _write(
            data / "spreads.json",
            {
                "months": spread_months,
                "pairs": [{"a": a, "b": b, **vals} for (a, b), vals in pairs.items()],
            },
        )
    sizes["overview"] = _write(data / "overview.json", _overview(con))
    sizes["map"] = _write(data / "map.json", _map_series(con))
    return {
        "out_dir": str(out_dir),
        "files": len(sizes),
        "zones": len(per_zone),
        "data_kb": round(sum(sizes.values()) / 1024),
        "dq_run_id": dq_run,
    }


MAP_DAYS = 365
MIN_ZONES_FOR_HEADLINE = 25


def _overview(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Headline numbers for the newest local day that most zones have prices for."""
    row = con.execute(
        "SELECT max(local_date) FROM (SELECT local_date FROM zone_daily"
        " WHERE price_coverage >= 0.95 GROUP BY 1 HAVING count(*) >= ?)",
        [MIN_ZONES_FOR_HEADLINE],
    ).fetchone()
    if not row or row[0] is None:
        return {"day": None}
    day = row[0]
    cursor = con.execute(
        "SELECT zone_key, price_eur_mean, price_eur_min, price_eur_max, negative_price_hours,"
        " load_mw_mean, price_coverage, load_coverage FROM zone_daily WHERE local_date = ?"
        " ORDER BY zone_key",
        [day],
    )
    cols = [d[0] for d in cursor.description]
    today = [dict(zip(cols, r, strict=True)) for r in cursor.fetchall()]
    priced = [z for z in today if z["price_eur_mean"] is not None and z["price_coverage"] >= 0.95]
    load_day_row = con.execute(
        "SELECT max(local_date) FROM (SELECT local_date FROM zone_daily"
        " WHERE load_coverage >= 0.95 GROUP BY 1 HAVING count(*) >= ?)",
        [MIN_ZONES_FOR_HEADLINE],
    ).fetchone()
    load_day = load_day_row[0] if load_day_row else None
    loaded = []
    if load_day is not None:
        loaded = [
            {"zone_key": k, "load_mw_mean": v}
            for k, v in con.execute(
                "SELECT zone_key, load_mw_mean FROM zone_daily"
                " WHERE local_date = ? AND load_coverage >= 0.95 AND load_mw_mean IS NOT NULL",
                [load_day],
            ).fetchall()
        ]

    def mean_by_day(days: int) -> list[tuple[Any, float]]:
        return con.execute(
            "SELECT local_date, avg(price_eur_mean) FROM zone_daily"
            " WHERE price_coverage >= 0.95 AND local_date > ? - ? AND local_date <= ?"
            " GROUP BY 1 HAVING count(*) >= ? ORDER BY 1",
            [day, days, day, MIN_ZONES_FOR_HEADLINE],
        ).fetchall()

    series = mean_by_day(90)
    previous = [v for d, v in series if (day - d).days in range(1, 8)]
    mean_today = sum(z["price_eur_mean"] for z in priced) / len(priced)
    weekly = sum(previous) / len(previous) if previous else None
    cheapest = min(priced, key=lambda z: z["price_eur_mean"])
    dearest = max(priced, key=lambda z: z["price_eur_mean"])
    negative = [z for z in today if (z["negative_price_hours"] or 0) > 0]
    spread = con.execute(
        "SELECT zone_a, zone_b, max(abs_spread_eur), arg_max(hour_utc, abs_spread_eur)"
        " FROM cross_zone_spread_hourly WHERE CAST(timezone('Europe/Brussels', hour_utc) AS DATE)"
        " = ? GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 1",
        [day],
    ).fetchone()
    record = con.execute(
        "SELECT zone_key, CAST(local_date AS VARCHAR), negative_price_hours FROM zone_daily"
        " ORDER BY negative_price_hours DESC NULLS LAST LIMIT 1"
    ).fetchone()
    return {
        "day": str(day),
        "zones_priced": len(priced),
        "price_mean": round(mean_today, 2),
        "price_week_mean": None if weekly is None else round(weekly, 2),
        "price_series": [[str(d), round(v, 2)] for d, v in series],
        "cheapest": {"zone": cheapest["zone_key"], "price": round(cheapest["price_eur_mean"], 2)},
        "dearest": {"zone": dearest["zone_key"], "price": round(dearest["price_eur_mean"], 2)},
        "negative_zones": [z["zone_key"] for z in negative],
        "negative_hours_total": sum(z["negative_price_hours"] or 0 for z in negative),
        "load_day": None if load_day is None else str(load_day),
        "load_total_mw": round(sum(z["load_mw_mean"] for z in loaded)) if loaded else None,
        "zones_loaded": len(loaded),
        "spread": None if not spread else {
            "a": spread[0], "b": spread[1], "value": round(float(spread[2]), 1),
            "hour": str(spread[3]),
        },
        "negative_record": None if not record else {
            "zone": record[0], "day": record[1], "hours": record[2]},
    }


def _map_series(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Daily values per zone for the last year, as aligned arrays for the map slider."""
    last = con.execute("SELECT max(local_date) FROM zone_daily").fetchone()
    assert last is not None
    rows = con.execute(
        "SELECT zone_key, CAST(local_date AS VARCHAR), price_eur_mean, load_mw_mean,"
        " negative_price_hours, least(1.0, coalesce(price_coverage, 0)),"
        " least(1.0, coalesce(load_coverage, 0)) FROM zone_daily"
        " WHERE local_date > ? - ? ORDER BY 1, 2",
        [last[0], MAP_DAYS],
    ).fetchall()
    dates = sorted({r[1] for r in rows})
    index = {d: i for i, d in enumerate(dates)}
    zones: dict[str, dict[str, list[Any]]] = {}
    for zone, day, price, load, neg, pcov, lcov in rows:
        entry = zones.setdefault(zone, {
            "price": [None] * len(dates), "load": [None] * len(dates),
            "neg": [None] * len(dates), "pcov": [None] * len(dates),
            "lcov": [None] * len(dates)})
        i = index[day]
        entry["price"][i] = None if price is None else round(float(price), 1)
        entry["load"][i] = None if load is None else round(float(load))
        entry["neg"][i] = neg
        entry["pcov"][i] = round(float(pcov), 2)
        entry["lcov"][i] = round(float(lcov), 2)
    return {"dates": dates, "zones": zones}
