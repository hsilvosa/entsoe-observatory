from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from conftest import Pipeline
from xmlgen import DocSpec, PeriodSpec, SeriesSpec

from entsoe_observatory.dq import DataQualityError, run_checks

PRICES = "day_ahead_prices"
LOAD = "actual_load"
Run = Callable[..., Pipeline]
NOW = datetime(2025, 11, 1, 12, 0, tzinfo=UTC)


def load_doc(start: str, end: str, points: dict[int, float], curve: str = "A03") -> DocSpec:
    period = PeriodSpec(start, end, "PT15M", points)
    return DocSpec([SeriesSpec([period], curve=curve)], start, end, dataset=LOAD)


def results(report_path: Path) -> list[dict[str, object]]:
    cursor = duckdb.connect().execute(
        f"select * from read_parquet('{(report_path / 'check_results.parquet').as_posix()}')"
    )
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def find(rows: list[dict[str, object]], check: str, zone: str = "ES") -> dict[str, object]:
    return next(r for r in rows if r["check_name"] == check and r["zone_key"] == zone)


def test_dst_aware_completeness_flags_only_real_gaps(run_pipeline: Run) -> None:
    # 2025-10-26 is a 25 h day (100 quarter hours); A03 expansion makes it complete.
    full = load_doc("2025-10-25T22:00Z", "2025-10-26T23:00Z", {1: 10.0, 50: 12.0})
    # 2025-10-27: curve A01 with only 40 points published -> 40 of 96 slots.
    gap = load_doc("2025-10-26T23:00Z", "2025-10-27T23:00Z", {i: 11.0 for i in range(1, 41)}, "A01")
    p = run_pipeline([(LOAD, "ES", "a.xml", full), (LOAD, "ES", "b.xml", gap)])
    report = run_checks(p.settings, now=NOW, fail_on_blocking=False)
    row = find(results(report.path), "completeness")
    assert (row["failed_count"], row["total_count"]) == (1, 2)
    assert "2025-10-27" in str(row["detail"])
    days = duckdb.sql(
        f"select local_date::varchar, expected_minutes, status from "
        f"read_parquet('{(report.path / 'completeness_daily.parquet').as_posix()}') order by 1"
    ).fetchall()
    assert days == [("2025-10-26", 1500, "complete"), ("2025-10-27", 1440, "incomplete")]


def test_unexpected_currency_is_a_blocking_failure(run_pipeline: Run) -> None:
    period = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T23:00Z", "PT60M", {1: 10.0})
    doc = DocSpec([SeriesSpec([period], currency="USD")], "2025-01-01T23:00Z", "2025-01-02T23:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", doc)])
    with pytest.raises(DataQualityError) as info:
        run_checks(p.settings, now=NOW)
    assert "currency_consistency" in str(info.value)


def test_duplicate_business_key_is_a_blocking_failure(run_pipeline: Run) -> None:
    doc = load_doc("2025-01-01T23:00Z", "2025-01-02T23:00Z", {1: 10.0})
    p = run_pipeline([(LOAD, "ES", "a.xml", doc)])
    part = next((p.settings.layer_dir("silver", LOAD) / "zone=ES").rglob("*.parquet"))
    shutil.copy(part, part.with_name("copy.parquet"))
    with pytest.raises(DataQualityError) as info:
        run_checks(p.settings, now=NOW)
    assert "duplicate_business_key" in str(info.value)


def test_clean_data_passes_and_writes_summary(run_pipeline: Run) -> None:
    doc = load_doc("2025-01-01T23:00Z", "2025-01-02T23:00Z", {1: 10.0})
    p = run_pipeline([(LOAD, "ES", "a.xml", doc)])
    report = run_checks(p.settings, now=NOW)
    assert report.blocking_failures == []
    assert (p.settings.layer_dir("dq") / "latest.json").exists()


def test_negative_load_is_flagged_not_blocking(run_pipeline: Run) -> None:
    doc = load_doc("2025-01-01T23:00Z", "2025-01-02T23:00Z", {1: -5.0})
    p = run_pipeline([(LOAD, "ES", "a.xml", doc)])
    report = run_checks(p.settings, now=NOW)
    row = find(results(report.path), "invalid_values")
    assert row["failed_count"] == 96
    assert row["status"] == "fail"


def test_resolution_and_currency_changes_are_reported(run_pipeline: Run) -> None:
    hourly = PeriodSpec("2025-09-30T22:00Z", "2025-10-01T22:00Z", "PT60M", {1: 50.0})
    quarter = PeriodSpec("2025-10-01T22:00Z", "2025-10-02T22:00Z", "PT15M", {1: 60.0})
    doc = DocSpec([SeriesSpec([hourly, quarter])], "2025-09-30T22:00Z", "2025-10-02T22:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", doc)])
    report = run_checks(p.settings, now=NOW)
    row = find(results(report.path), "resolution_changes")
    assert "PT60M -> PT15M on 2025-10-02" in str(row["detail"])
    assert row["status"] == "pass"


def test_freshness_warns_when_data_is_old(run_pipeline: Run) -> None:
    doc = load_doc("2025-01-01T23:00Z", "2025-01-02T23:00Z", {1: 10.0})
    p = run_pipeline([(LOAD, "ES", "a.xml", doc)])
    stale = run_checks(p.settings, now=datetime(2025, 6, 1, tzinfo=UTC))
    assert find(results(stale.path), "freshness")["status"] == "fail"
    fresh = run_checks(p.settings, now=datetime(2025, 1, 2, 23, 30, tzinfo=UTC))
    assert find(results(fresh.path), "freshness")["status"] == "pass"
