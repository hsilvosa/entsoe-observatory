from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import Pipeline
from xmlgen import DocSpec, PeriodSpec, SeriesSpec

PRICES = "day_ahead_prices"
LOAD = "actual_load"
Run = Callable[..., Pipeline]


def doc(series: list[SeriesSpec], start: str, end: str, **kw: object) -> DocSpec:
    return DocSpec(series=series, request_start=start, request_end=end, **kw)  # type: ignore[arg-type]


def test_a03_expansion_on_dst_day_spring(run_pipeline: Run) -> None:
    # ES 2024-03-31 is a 23 h day; only 12 points are published.
    pts = {1: 1.0, 2: 2.0, 3: 3.0, 6: 6.0, 10: 10.0}
    period = PeriodSpec("2024-03-30T23:00Z", "2024-03-31T22:00Z", "PT60M", pts)
    d = doc([SeriesSpec([period])], "2024-03-30T23:00Z", "2024-03-31T22:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", d)])
    con = p.silver(PRICES)
    n, filled = con.execute("select count(*), sum(is_a03_filled::int) from s").fetchone()  # type: ignore[misc]
    assert (n, filled) == (23, 18)
    values = con.execute("select value from s order by timestamp_utc").fetchall()
    assert [v[0] for v in values][:7] == [1.0, 2.0, 3.0, 3.0, 3.0, 6.0, 6.0]


@pytest.mark.parametrize(
    ("day_start", "day_end", "expected"),
    [
        ("2025-03-29T23:00Z", "2025-03-30T22:00Z", 92),
        ("2025-10-25T22:00Z", "2025-10-26T23:00Z", 100),
        ("2025-06-01T22:00Z", "2025-06-02T22:00Z", 96),
    ],
)
def test_load_local_day_has_dst_aware_slot_count(
    run_pipeline: Run, day_start: str, day_end: str, expected: int
) -> None:
    period = PeriodSpec(day_start, day_end, "PT15M", {1: 100.0, 40: 120.0})
    d = doc([SeriesSpec([period])], day_start, day_end, dataset=LOAD)
    p = run_pipeline([(LOAD, "ES", "a.xml", d)])
    con = p.silver(LOAD)
    days = con.execute("select local_date, count(*) from s group by 1").fetchall()
    assert len(days) == 1
    assert days[0][1] == expected
    assert con.execute("select min(unit) from s").fetchone() == ("MW",)


def test_mixed_resolution_is_kept_not_merged(run_pipeline: Run) -> None:
    hourly = PeriodSpec("2025-09-30T22:00Z", "2025-10-01T22:00Z", "PT60M", {1: 50.0})
    quarter = PeriodSpec("2025-10-01T22:00Z", "2025-10-02T22:00Z", "PT15M", {1: 60.0})
    d = doc([SeriesSpec([hourly, quarter])], "2025-09-30T22:00Z", "2025-10-02T22:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", d)])
    rows = p.silver(PRICES).execute(
        "select resolution, count(*) from s group by 1 order by 1"
    ).fetchall()
    assert rows == [("PT15M", 96), ("PT60M", 24)]


def test_parallel_sequences_are_distinct_series(run_pipeline: Run) -> None:
    period = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T23:00Z", "PT60M", {1: 10.0})
    p2 = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T23:00Z", "PT60M", {1: 99.0})
    d = doc(
        [SeriesSpec([period], sequence=1), SeriesSpec([p2], sequence=2)],
        "2025-01-01T23:00Z",
        "2025-01-02T23:00Z",
    )
    p = run_pipeline([(PRICES, "AT", "a.xml", d)])
    con = p.silver(PRICES)
    assert con.execute("select count(*) from s").fetchone() == (48,)
    rows = con.execute(
        "select sequence, min(value), max(value) from s group by 1 order by 1"
    ).fetchall()
    assert rows == [(1, 10.0, 10.0), (2, 99.0, 99.0)]


def test_newest_revision_wins(run_pipeline: Run) -> None:
    def make(rev: int, value: float, name: str) -> DocSpec:
        period = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T00:00Z", "PT60M", {1: value})
        return doc(
            [SeriesSpec([period])],
            "2025-01-01T23:00Z",
            "2025-01-02T00:00Z",
            revision=rev,
            mrid=name,
        )

    p = run_pipeline(
        [
            (PRICES, "ES", "a.xml", make(2, 22.0, "new")),
            (PRICES, "ES", "b.xml", make(1, 11.0, "old")),
        ]
    )
    con = p.silver(PRICES)
    assert con.execute("select value, revision_number from s").fetchall() == [(22.0, 2)]
    assert p.summary["duplicates_dropped"] == 1


def test_real_point_beats_a03_filled_duplicate(run_pipeline: Run) -> None:
    # Adjacent monthly files overlap on one day: one has the slot filled, the other real.
    filled = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T01:00Z", "PT60M", {1: 5.0})
    real = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T01:00Z", "PT60M", {1: 5.0, 2: 7.0})
    a = doc([SeriesSpec([filled])], "2025-01-01T23:00Z", "2025-01-02T01:00Z")
    b = doc([SeriesSpec([real])], "2025-01-01T23:00Z", "2025-01-02T01:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", a), (PRICES, "ES", "b.xml", b)])
    con = p.silver(PRICES)
    assert con.execute(
        "select value, is_a03_filled from s order by timestamp_utc"
    ).fetchall() == [(5.0, False), (7.0, False)]


def test_validity_rules(run_pipeline: Run) -> None:
    load = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T01:00Z", "PT60M", {1: -5.0, 2: 10.0})
    pl = doc([SeriesSpec([load])], "2025-01-01T23:00Z", "2025-01-02T01:00Z", dataset=LOAD)
    price = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T01:00Z", "PT60M", {1: -50.0, 2: 10.0})
    pp = doc([SeriesSpec([price])], "2025-01-01T23:00Z", "2025-01-02T01:00Z")
    p = run_pipeline([(LOAD, "ES", "l.xml", pl), (PRICES, "ES", "p.xml", pp)])
    assert p.silver(LOAD).execute(
        "select quality_flag, is_valid from s order by timestamp_utc"
    ).fetchall() == [("negative_load", False), ("ok", True)]
    assert p.silver(PRICES).execute(
        "select quality_flag, is_valid, unit from s order by timestamp_utc"
    ).fetchall() == [("ok", True, "EUR/MWh"), ("ok", True, "EUR/MWh")]


def test_out_of_window_rows_are_dropped_and_counted(run_pipeline: Run) -> None:
    period = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T03:00Z", "PT60M", {1: 1.0})
    d = doc([SeriesSpec([period])], "2025-01-01T23:00Z", "2025-01-02T01:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", d)])
    assert p.summary["out_of_window_dropped"] == 2
    assert p.summary["silver_rows"] == 2


def test_rebuild_is_idempotent(run_pipeline: Run) -> None:
    period = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T23:00Z", "PT60M", {1: 1.0})
    d = doc([SeriesSpec([period])], "2025-01-01T23:00Z", "2025-01-02T23:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", d)])
    from entsoe_observatory.bronze import build_bronze, new_run_id
    from entsoe_observatory.silver import build_silver

    run_id = new_run_id()
    build_bronze(p.settings, run_id)
    summary = build_silver(p.settings, run_id)
    assert summary["silver_rows"] == 24
    assert p.silver(PRICES).execute("select count(*) from s").fetchone() == (24,)


def test_non_a01_contract_and_acknowledgement_are_skipped(run_pipeline: Run) -> None:
    period = PeriodSpec("2025-01-01T23:00Z", "2025-01-02T00:00Z", "PT60M", {1: 1.0})
    weekly = SeriesSpec([period], contract_type="A02")
    daily = SeriesSpec([period])
    d = doc([weekly, daily], "2025-01-01T23:00Z", "2025-01-02T00:00Z")
    p = run_pipeline([(PRICES, "ES", "a.xml", d)])
    assert p.summary["silver_rows"] == 1
