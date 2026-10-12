from datetime import date, timedelta

from entsoe_observatory.insights import HORIZONS, forecast_from, forecast_zone


def _series(days: int, noise: bool = False) -> tuple[list[float | None], list[date]]:
    start = date(2023, 1, 2)  # a Monday
    dates = [start + timedelta(days=i) for i in range(days)]
    base = [10.0, 12.0, 14.0, 13.0, 11.0, 4.0, 2.0]
    values: list[float | None] = [
        base[d.weekday()] + (((i * 7919) % 13) - 6 if noise else 0) for i, d in enumerate(dates)
    ]
    return values, dates


def test_forecast_recovers_a_pure_weekly_pattern() -> None:
    values, dates = _series(300)
    origin = len(values) - 1
    pred = forecast_from(values, dates, origin)
    assert pred is not None
    base = [10.0, 12.0, 14.0, 13.0, 11.0, 4.0, 2.0]
    expected = [base[(dates[origin].weekday() + h) % 7] for h in range(1, HORIZONS + 1)]
    assert all(abs(p - e) < 1e-9 for p, e in zip(pred, expected, strict=True))


def test_forecast_ignores_values_after_the_origin() -> None:
    values, dates = _series(300, noise=True)
    origin = 250
    before = forecast_from(values, dates, origin)
    changed = values[: origin + 1] + [999.0] * (len(values) - origin - 1)
    assert forecast_from(changed, dates, origin) == before


def test_forecast_needs_history() -> None:
    values, dates = _series(20)
    assert forecast_from(values, dates, 19) is None
    assert forecast_zone(values, dates) is None


def test_forecast_zone_reports_backtest_and_interval() -> None:
    values, dates = _series(900, noise=True)
    result = forecast_zone(values, dates)
    assert result is not None
    assert len(result["forecast"]) == HORIZONS
    assert all(r["lo"] <= r["mid"] <= r["hi"] for r in result["forecast"])
    assert 0.5 < result["coverage"] <= 1.0
    # With a strong weekly pattern the model beats repeating last week's value.
    mae = result["mae"]
    assert sum(mae["model"]) < sum(mae["persist"])
