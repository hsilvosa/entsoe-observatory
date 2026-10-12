"""Derived views for the dashboard: market patterns and a short-horizon price forecast.

Everything here is computed from gold tables. The forecast is deliberately simple (a rolling
level plus a weekday effect, no fitted parameters) so that it can be checked against a naive
baseline. It is a demonstration of the data, not a trading signal.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import duckdb

HORIZONS = 7
LEVEL_SHORT, LEVEL_LONG = 7, 28
EFFECT_WEEKS = 26
BACKTEST_DAYS, EVAL_DAYS = 270, 90
HISTORY_DAYS = 45
MIN_PAIRS = 400
INTERVAL = (0.10, 0.90)

DUCK_ZONES = ("ES", "DE_LU", "FR", "NL", "IT_NORD", "GR", "PL", "HU", "BE", "DK_1")
MIN_WEATHER_ROWS = 8000


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _quantile(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def forecast_from(
    values: list[float | None], dates: list[date], origin: int
) -> list[float] | None:
    """Forecast the next `HORIZONS` days using only `values[: origin + 1]`.

    Level is the mean of the last 7 and last 28 days; the weekday effect is the mean deviation of
    each weekday from its calendar-week mean over the last 26 complete weeks. Returns None when
    there is too little history.
    """
    short = _mean(values[max(0, origin - LEVEL_SHORT + 1) : origin + 1])
    long = _mean(values[max(0, origin - LEVEL_LONG + 1) : origin + 1])
    if short is None or long is None:
        return None
    level = 0.5 * short + 0.5 * long
    last_sunday = origin - ((dates[origin].weekday() + 1) % 7)
    deviations: dict[int, list[float]] = {w: [] for w in range(7)}
    for week in range(EFFECT_WEEKS):
        end = last_sunday - 7 * week
        start = end - 6
        if start < 0:
            break
        block = values[start : end + 1]
        if any(v is None for v in block):
            continue
        week_mean = sum(v for v in block if v is not None) / 7
        for offset, v in enumerate(block):
            if v is not None:
                deviations[(dates[start].weekday() + offset) % 7].append(v - week_mean)
    if any(len(d) < 8 for d in deviations.values()):
        return None
    effect = {w: sum(d) / len(d) for w, d in deviations.items()}
    return [
        level + effect[(dates[origin].weekday() + h) % 7] for h in range(1, HORIZONS + 1)
    ]


def forecast_zone(
    values: list[float | None], dates: list[date]
) -> dict[str, Any] | None:
    """Backtest and live forecast for one zone, or None when there is not enough history."""
    last = len(values) - 1
    pairs: list[tuple[int, int, float, float, float | None]] = []  # h, target, y, pred, naive
    persist: dict[tuple[int, int], float] = {}
    for origin in range(max(0, last - BACKTEST_DAYS), last):
        path = forecast_from(values, dates, origin)
        base = _mean(values[max(0, origin - LEVEL_SHORT + 1) : origin + 1])
        if path is None or base is None:
            continue
        for h in range(1, HORIZONS + 1):
            target = origin + h
            y = values[target] if target <= last else None
            if y is None:
                continue
            pairs.append((h, target, y, path[h - 1], values[target - 7]))
            persist[(h, target)] = base
    if len(pairs) < MIN_PAIRS:
        return None
    eval_start = last - EVAL_DAYS
    residuals: list[list[float]] = [[] for _ in range(HORIZONS)]
    for h, target, y, pred, _ in pairs:
        if target < eval_start:
            residuals[h - 1].append(y - pred)
    if any(len(r) < 30 for r in residuals):
        return None
    cal = [
        (_quantile(sorted(r), INTERVAL[0]), _quantile(sorted(r), INTERVAL[1])) for r in residuals
    ]
    err: dict[str, list[list[float]]] = {
        k: [[] for _ in range(HORIZONS)] for k in ("model", "naive", "persist")
    }
    inside = total = 0
    for h, target, y, pred, naive in pairs:
        if target < eval_start or naive is None:
            continue
        err["model"][h - 1].append(abs(y - pred))
        err["naive"][h - 1].append(abs(y - naive))
        err["persist"][h - 1].append(abs(y - persist[(h, target)]))
        total += 1
        inside += pred + cal[h - 1][0] <= y <= pred + cal[h - 1][1]
    if total < 100:
        return None
    live = forecast_from(values, dates, last)
    if live is None:
        return None
    full = [[y - p for hh, _, y, p, _ in pairs if hh == h] for h in range(1, HORIZONS + 1)]
    bands = [(_quantile(sorted(r), INTERVAL[0]), _quantile(sorted(r), INTERVAL[1])) for r in full]
    start = max(0, last - HISTORY_DAYS + 1)
    return {
        "origin": dates[last].isoformat(),
        "history": {
            "dates": [d.isoformat() for d in dates[start:]],
            "values": [None if v is None else round(v, 1) for v in values[start:]],
        },
        "forecast": [
            {
                "date": (dates[last] + timedelta(days=h)).isoformat(),
                "mid": round(live[h - 1], 1),
                "lo": round(live[h - 1] + bands[h - 1][0], 1),
                "hi": round(live[h - 1] + bands[h - 1][1], 1),
            }
            for h in range(1, HORIZONS + 1)
        ],
        "mae": {
            k: [round(sum(e) / len(e), 2) if e else None for e in per_h]
            for k, per_h in err.items()
        },
        "coverage": round(inside / total, 3),
        "eval_pairs": total,
    }


def build_forecast(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        "SELECT zone_key, local_date, price_eur_mean FROM zone_daily"
        " WHERE price_coverage >= 0.95 AND price_eur_mean IS NOT NULL ORDER BY 1, 2"
    ).fetchall()
    per_zone: dict[str, dict[date, float]] = {}
    for zone, day, price in rows:
        per_zone.setdefault(zone, {})[day] = float(price)
    if not per_zone:
        return {"horizons": HORIZONS, "interval": [int(q * 100) for q in INTERVAL], "zones": {}}
    newest = max(max(s) for s in per_zone.values())
    zones: dict[str, Any] = {}
    for zone, series in per_zone.items():
        first, last = min(series), max(series)
        if (newest - last).days > 10 or (last - first).days < 800:
            continue
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        result = forecast_zone([series.get(d) for d in days], days)
        if result is not None:
            zones[zone] = result
    return {"horizons": HORIZONS, "interval": [int(q * 100) for q in INTERVAL], "zones": zones}


def build_patterns(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Hourly shape of prices, weekday effect, and price response to wind and sun."""
    latest = con.execute("SELECT max(local_date) FROM zone_daily").fetchone()
    assert latest is not None and latest[0] is not None
    since = latest[0] - timedelta(days=3 * 365)

    shape_rows = con.execute(
        "SELECT zone_key, year(local_date), hour(hour_local), avg(price_eur_mean), count(*)"
        " FROM price_hourly WHERE month(local_date) IN (6, 7, 8) AND zone_key IN"
        f" ({','.join(repr(z) for z in DUCK_ZONES)}) AND price_eur_mean IS NOT NULL"
        " GROUP BY ALL ORDER BY 1, 2, 3"
    ).fetchall()
    shapes: dict[str, dict[str, list[float | None]]] = {}
    for zone, year, hour, mean, count in shape_rows:
        if count < 60:
            continue
        shapes.setdefault(zone, {}).setdefault(str(year), [None] * 24)[hour] = round(mean, 1)
    shapes = {z: {y: v for y, v in ys.items() if sum(x is not None for x in v) == 24}
              for z, ys in shapes.items()}

    week_rows = con.execute(
        "SELECT zone_key, isodow(local_date), avg(price_eur_mean) FROM zone_daily"
        " WHERE local_date > ? AND price_coverage >= 0.95 AND price_eur_mean IS NOT NULL"
        " GROUP BY ALL ORDER BY 1, 2",
        [since],
    ).fetchall()
    by_zone: dict[str, dict[int, float]] = {}
    for zone, dow, mean in week_rows:
        by_zone.setdefault(zone, {})[dow] = mean
    weekday: dict[str, dict[str, Any]] = {}
    for zone, days in by_zone.items():
        if len(days) != 7:
            continue
        overall = sum(days.values()) / 7
        if overall < 5:
            continue
        work = sum(days[d] for d in range(1, 6)) / 5
        weekend = (days[6] + days[7]) / 2
        weekday[zone] = {
            "index": [round(days[d] - overall, 2) for d in range(1, 8)],
            "weekend_gap_pct": round((weekend / work - 1) * 100, 1),
            "mean": round(overall, 1),
        }

    response: dict[str, Any] = {}
    listing = con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    tables = {r[0] for r in listing}
    for kind, column, extra in () if "weather_hourly" not in tables else (
        ("wind", "wind_speed_10m", ""),
        ("sun", "shortwave_radiation", " AND w.shortwave_radiation > 0"),
    ):
        data = con.execute(
            f"""
            WITH j AS (
              SELECT p.zone_key, strftime(p.hour_utc, '%Y-%m') AS ym,
                     p.price_eur_mean AS price, w.{column} AS x
              FROM price_hourly p JOIN weather_hourly w
                ON p.zone_key = w.zone_key AND p.hour_utc = w.hour_utc
              WHERE p.local_date > ? AND p.price_eur_mean IS NOT NULL
                AND w.{column} IS NOT NULL{extra}
            ), d AS (
              SELECT zone_key, price - avg(price) OVER (PARTITION BY zone_key, ym) AS dp,
                     x - avg(x) OVER (PARTITION BY zone_key, ym) AS dx FROM j
            ), b AS (
              SELECT zone_key, dp, dx, ntile(5) OVER (PARTITION BY zone_key ORDER BY dx) AS q
              FROM d
            ), per_zone AS (
              SELECT zone_key, corr(dp, dx) AS r, count(*) AS n FROM d GROUP BY zone_key
            )
            SELECT b.zone_key, b.q, avg(b.dp), avg(b.dx), any_value(z.r), any_value(z.n)
            FROM b JOIN per_zone z USING (zone_key) GROUP BY b.zone_key, b.q ORDER BY 1, 2
            """,
            [since],
        ).fetchall()
        out: dict[str, Any] = {}
        for zone, q, dp, dx, r, n in data:
            if n < MIN_WEATHER_ROWS or r is None:
                continue
            entry = out.setdefault(zone, {"r": round(r, 3), "dp": [None] * 5, "dx": [None] * 5})
            entry["dp"][q - 1] = round(dp, 1)
            entry["dx"][q - 1] = round(dx, 2)
        response[kind] = out
    return {
        "since": since.isoformat(),
        "until": latest[0].isoformat(),
        "duck": shapes,
        "weekday": weekday,
        "response": response,
    }
