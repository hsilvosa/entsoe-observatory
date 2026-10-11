from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import Pipeline
from xmlgen import DocSpec, PeriodSpec, SeriesSpec

from entsoe_observatory.dq import run_checks
from entsoe_observatory.gold import (
    CONTRACT_DIR,
    INNER_SQL,
    TABLE_ORDER,
    Contract,
    build_gold,
    load_contract,
    validate_files,
)

PRICES = "day_ahead_prices"
LOAD = "actual_load"
Run = Callable[..., Pipeline]
NOW = datetime(2025, 5, 1, tzinfo=UTC)


def price_doc(
    start: str, end: str, points: dict[int, float], *, res: str = "PT60M", currency: str = "EUR",
    curve: str = "A03", sequence: int | None = None,
) -> DocSpec:
    period = PeriodSpec(start, end, res, points)
    series = SeriesSpec([period], curve=curve, currency=currency, sequence=sequence)
    return DocSpec([series], start, end)


def write_fx(pipeline: Pipeline, rows: list[tuple[str, str, float]]) -> None:
    directory = pipeline.settings.layer_dir("silver", "fx_daily")
    directory.mkdir(parents=True)
    values = ", ".join(f"('{c}', DATE '{d}', {r}, true)" for c, d, r in rows)
    duckdb.sql(
        f"COPY (SELECT * FROM (VALUES {values}) t(currency, rate_date, units_per_eur, is_valid))"
        f" TO '{(directory / 'fx_daily.parquet').as_posix()}' (FORMAT parquet)"
    )


def gold(pipeline: Pipeline, table: str) -> list[dict[str, Any]]:
    glob = (pipeline.settings.layer_dir("gold", table) / "**" / "*.parquet").as_posix()
    cursor = duckdb.connect().execute(
        f"SELECT * FROM read_parquet('{glob}', hive_partitioning=false)"
    )
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def build(pipeline: Pipeline) -> dict[str, Any]:
    run_checks(pipeline.settings, now=NOW, fail_on_blocking=False)
    return build_gold(pipeline.settings)


@pytest.fixture
def scenario(run_pipeline: Run) -> Pipeline:
    dst = ("2025-03-29T23:00Z", "2025-03-30T22:00Z")  # 23 h local day in Spain
    es = price_doc(*dst, {1: -10.0, 10: 30.0})
    pt = price_doc(*dst, {1: 5.0})
    pl_a = price_doc("2025-03-29T23:00Z", "2025-03-30T22:00Z", {1: 400.0}, currency="PLN")
    pl_b = price_doc("2025-04-09T22:00Z", "2025-04-10T22:00Z", {1: 800.0}, currency="PLN")
    at = DocSpec(
        [
            SeriesSpec([PeriodSpec("2025-03-31T22:00Z", "2025-04-01T22:00Z", "PT60M", {1: 50.0})],
                       sequence=1),
            SeriesSpec([PeriodSpec("2025-03-31T22:00Z", "2025-04-01T22:00Z", "PT15M", {1: 999.0})],
                       sequence=2),
        ],
        "2025-03-31T22:00Z",
        "2025-04-01T22:00Z",
    )
    quarter = PeriodSpec("2025-10-01T22:00Z", "2025-10-02T22:00Z", "PT15M",
                         {i: float(i * 10) for i in range(1, 8)})
    es_quarter = DocSpec([SeriesSpec([quarter], curve="A01")], "2025-10-01T22:00Z",
                         "2025-10-02T22:00Z")
    load = DocSpec(
        [SeriesSpec([PeriodSpec(dst[0], dst[1], "PT15M", {1: 1000.0, 50: 1200.0})])],
        dst[0], dst[1], dataset=LOAD,
    )
    pipeline = run_pipeline(
        [
            (PRICES, "ES", "a.xml", es), (PRICES, "ES", "b.xml", es_quarter),
            (PRICES, "PT", "c.xml", pt), (PRICES, "PL", "d.xml", pl_a),
            (PRICES, "PL", "e.xml", pl_b), (PRICES, "AT", "f.xml", at),
            (LOAD, "ES", "g.xml", load),
        ]
    )
    write_fx(pipeline, [("PLN", "2025-03-28", 4.0)])
    build(pipeline)
    return pipeline


def test_all_tables_are_built_under_their_contracts(scenario: Pipeline) -> None:
    for table in TABLE_ORDER:
        if table == "weather_hourly":
            continue  # no weather in the scenario
        assert gold(scenario, table), table


def test_eur_conversion_uses_last_rate_within_five_days(scenario: Pipeline) -> None:
    rows = {(r["zone_key"], r["local_date"].isoformat()): r for r in gold(scenario, "price_native")
            if r["zone_key"] == "PL"}
    sunday = rows[("PL", "2025-03-30")]  # Friday rate used on Sunday
    assert (sunday["fx_status"], sunday["price_eur"], sunday["fx_rate_date"].isoformat()) == (
        "converted", 100.0, "2025-03-28")
    stale = rows[("PL", "2025-04-10")]  # 13 days after the last rate
    assert (stale["fx_status"], stale["price_eur"], stale["fx_units_per_eur"]) == (
        "missing_rate", None, None)
    es = next(r for r in gold(scenario, "price_native") if r["zone_key"] == "ES")
    assert es["fx_status"] == "native" and es["price_eur"] == es["price_local"]


def test_only_the_sdac_sequence_reaches_gold_prices(scenario: Pipeline) -> None:
    at = [r for r in gold(scenario, "price_native") if r["zone_key"] == "AT"]
    assert {r["sequence"] for r in at} == {1}
    assert {r["price_local"] for r in at} == {50.0}


def test_hourly_mean_keeps_native_slots_and_reports_gaps(scenario: Pipeline) -> None:
    hours = {r["hour_local"].isoformat(): r for r in gold(scenario, "price_hourly")
             if r["zone_key"] == "ES" and r["source_resolution"] == "PT15M"}
    first = hours["2025-10-02T00:00:00"]
    counts = (first["slots_present"], first["slots_expected"], first["price_eur_mean"])
    assert counts == (4, 4, 25.0)
    partial = hours["2025-10-02T01:00:00"]  # positions 5-7 only
    assert (partial["slots_present"], partial["slots_expected"]) == (3, 4)
    hourly = next(r for r in gold(scenario, "price_hourly")
                  if r["zone_key"] == "ES" and r["source_resolution"] == "PT60M")
    assert (hourly["slots_present"], hourly["slots_expected"]) == (1, 1)


def test_zone_daily_on_a_dst_day(scenario: Pipeline) -> None:
    day = next(r for r in gold(scenario, "zone_daily")
               if r["zone_key"] == "ES" and r["local_date"].isoformat() == "2025-03-30")
    assert day["hours_in_day"] == 23
    assert day["negative_price_hours"] == 9
    assert (day["price_eur_min"], day["price_eur_max"]) == (-10.0, 30.0)
    assert day["price_eur_peak_mean"] is None  # Sunday: everything is off-peak
    assert day["price_coverage"] == pytest.approx(1.0)
    assert day["load_coverage"] == pytest.approx(1.0)


def test_spread_only_where_both_zones_have_a_complete_hour(scenario: Pipeline) -> None:
    spreads = [r for r in gold(scenario, "cross_zone_spread_hourly")
               if (r["zone_a"], r["zone_b"]) == ("ES", "PT")]
    assert len(spreads) == 23
    assert {round(r["spread_eur"], 1) for r in spreads} == {-15.0, 25.0}


def test_scorecard_summary_uses_the_primary_series(scenario: Pipeline) -> None:
    summary = next(r for r in gold(scenario, "dq_scorecard_summary")
                   if r["dataset"] == PRICES and r["zone_key"] == "AT")
    assert summary["blocking_checks_failed"] == 0
    assert summary["days_total"] == 1  # the sequence 2 series is not counted


def test_validate_files_catches_schema_nulls_duplicates_and_rules(tmp_path: Path) -> None:
    contract = Contract(
        name="t", version="1.0.0", description="t", grain=["k"], partition_by=[],
        columns=[
            {"name": "k", "type": "VARCHAR", "nullable": False, "description": "key"},
            {"name": "v", "type": "DOUBLE", "nullable": False, "description": "value"},
        ],
        constraints=["v >= 0"],
    )
    con = duckdb.connect()
    wrong = tmp_path / "wrong.parquet"
    con.execute(f"COPY (SELECT 1 AS x) TO '{wrong.as_posix()}' (FORMAT parquet)")
    assert "schema differs" in validate_files(con, contract, wrong.as_posix())[0]

    good = tmp_path / "good.parquet"
    con.execute(
        "COPY (SELECT * FROM (VALUES ('a', 1e0), ('b', 2e0)) t(k, v)) "
        f"TO '{good.as_posix()}' (FORMAT parquet)"
    )
    assert validate_files(con, contract, good.as_posix()) == []

    bad = tmp_path / "bad.parquet"
    con.execute(
        "COPY (SELECT * FROM (VALUES ('a', -1e0), ('a', 2e0), (NULL, 3e0)) t(k, v)) "
        f"TO '{bad.as_posix()}' (FORMAT parquet)"
    )
    problems = " | ".join(validate_files(con, contract, bad.as_posix()))
    assert "nulls in non-nullable column k" in problems
    assert "duplicate rows on grain (k)" in problems
    assert "1 rows violate: v >= 0" in problems


def test_every_contract_is_consistent_with_the_code() -> None:
    assert {p.stem for p in CONTRACT_DIR.glob("*.json")} == set(TABLE_ORDER) == set(INNER_SQL)
    for name in TABLE_ORDER:
        contract = load_contract(name)
        raw = json.loads((CONTRACT_DIR / f"{name}.json").read_text(encoding="utf-8"))
        names = contract.column_names
        assert len(names) == len(set(names)), name
        assert set(contract.grain) <= set(names), name
        assert set(contract.partition_by) <= set(names), name
        assert raw["version"].count(".") == 2
        assert all(c["description"] for c in contract.columns), name


def test_dashboard_exports_aligned_json(scenario: Pipeline, tmp_path: Path) -> None:
    from entsoe_observatory.dashboard import build_dashboard

    out = tmp_path / "site"
    summary = build_dashboard(scenario.settings, out)
    assert summary["zones"] >= 3
    for name in ("index.html", "app.js", "style.css"):
        assert (out / name).exists()
    zone = json.loads((out / "data" / "zones" / "ES.json").read_text(encoding="utf-8"))
    lengths = {len(v) for k, v in zone.items() if isinstance(v, list)}
    assert len(lengths) == 1  # columnar arrays stay aligned
    assert zone["negative_hours"][zone["dates"].index("2025-03-30")] == 9
    coverage = json.loads((out / "data" / "coverage.json").read_text(encoding="utf-8"))
    assert {r["dataset"] for r in coverage} == {PRICES, LOAD}
    spreads = json.loads((out / "data" / "spreads.json").read_text(encoding="utf-8"))
    assert {"a": "ES", "b": "PT"}.items() <= next(
        p for p in spreads["pairs"] if p["a"] == "ES" and p["b"] == "PT"
    ).items()
    heat = json.loads((out / "data" / "completeness_monthly.json").read_text(encoding="utf-8"))
    assert heat["months"] and PRICES in heat["series"]


def test_dashboard_requires_gold(tmp_path: Path) -> None:
    from entsoe_observatory.dashboard import build_dashboard
    from entsoe_observatory.settings import Settings

    settings = Settings(STORAGE_ROOT=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(FileNotFoundError):
        build_dashboard(settings, tmp_path / "site")
