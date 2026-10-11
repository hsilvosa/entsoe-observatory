from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import httpx

from entsoe_observatory.fx import non_eur_currencies, pull_fx
from entsoe_observatory.settings import Settings
from entsoe_observatory.weather import (
    WeatherClient,
    load_points,
    pull_weather,
    split_range,
)

TODAY = date(2026, 3, 20)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(STORAGE_ROOT=tmp_path / "storage", THREADS=2)  # type: ignore[call-arg]


def test_split_range_archive_then_forecast() -> None:
    assert split_range(date(2026, 3, 1), date(2026, 3, 10), TODAY) == [
        ("archive", date(2026, 3, 1), date(2026, 3, 10))
    ]
    assert split_range(date(2026, 3, 1), date(2026, 3, 20), TODAY) == [
        ("archive", date(2026, 3, 1), date(2026, 3, 13)),
        ("forecast", date(2026, 3, 14), date(2026, 3, 20)),
    ]
    assert split_range(date(2026, 3, 18), date(2026, 3, 20), TODAY) == [
        ("forecast", date(2026, 3, 18), date(2026, 3, 20))
    ]


def test_every_zone_has_weighted_points() -> None:
    points = load_points()
    assert len(points) == 51
    assert all(abs(sum(p.weight for p in pts) - 1) < 1e-9 for pts in points.values())


def points_file(tmp_path: Path) -> Path:
    path = tmp_path / "points.json"
    path.write_text(
        json.dumps(
            {
                "zones": {
                    "ES": [
                        {"name": "a", "latitude": 40.0, "longitude": -3.0, "weight": 0.75},
                        {"name": "b", "latitude": 41.0, "longitude": 2.0, "weight": 0.25},
                    ]
                }
            }
        )
    )
    return path


def payload(temp: list[float | None]) -> dict[str, object]:
    n = len(temp)
    return {
        "hourly": {
            "time": [f"2026-03-01T{h:02d}:00" for h in range(n)],
            "temperature_2m": temp,
            "wind_speed_10m": [3.0] * n,
            "relative_humidity_2m": [50.0] * n,
            "shortwave_radiation": [0.0] * n,
        }
    }


def test_weather_zone_mean_is_weighted_and_renormalised(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[payload([10.0, 10.0]), payload([30.0, None])])

    client = WeatherClient(transport=httpx.MockTransport(handler), pause_s=0, sleep=lambda s: None)
    settings = settings_for(tmp_path)
    summary = pull_weather(
        settings, client, start=date(2026, 3, 1), end=date(2026, 3, 1), zones=["ES"],
        today=TODAY, points_path=points_file(tmp_path),
    )
    assert summary["rows"] == 4
    assert seen[0].url.params["wind_speed_unit"] == "ms"
    assert seen[0].url.host == "archive-api.open-meteo.com"
    glob = (settings.layer_dir("silver", "weather") / "**" / "*.parquet").as_posix()
    rows = duckdb.sql(
        f"select temperature_2m, points_used from read_parquet('{glob}', hive_partitioning=false)"
        " order by timestamp_utc"
    ).fetchall()
    assert rows == [(15.0, 2), (10.0, 1)]  # 0.75*10+0.25*30 ; second hour only point a


def test_archive_beats_forecast_for_the_same_hour(tmp_path: Path) -> None:
    def make(temp: float):  # type: ignore[no-untyped-def]
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[payload([temp]), payload([temp])])

        return WeatherClient(
            transport=httpx.MockTransport(handler), pause_s=0, sleep=lambda s: None
        )

    settings = settings_for(tmp_path)
    kwargs = dict(
        start=date(2026, 3, 1), end=date(2026, 3, 1), zones=["ES"],
        points_path=points_file(tmp_path),
    )
    # First as "recent" (forecast API), later as history (archive).
    pull_weather(settings, make(20.0), today=date(2026, 3, 2), **kwargs)  # type: ignore[arg-type]
    pull_weather(settings, make(18.0), today=date(2026, 4, 1), **kwargs)  # type: ignore[arg-type]
    pull_weather(settings, make(25.0), today=date(2026, 3, 2), **kwargs)  # type: ignore[arg-type]
    glob = (settings.layer_dir("silver", "weather") / "**" / "*.parquet").as_posix()
    rows = duckdb.sql(
        f"select temperature_2m, source from read_parquet('{glob}', hive_partitioning=false)"
    ).fetchall()
    assert rows == [(18.0, "archive")]


ECB_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
    "EXR.D.PLN.EUR.SP00.A,D,PLN,EUR,SP00,A,2026-03-02,4.25\n"
    "EXR.D.PLN.EUR.SP00.A,D,PLN,EUR,SP00,A,2026-03-03,4.30\n"
    "EXR.D.RON.EUR.SP00.A,D,RON,EUR,SP00,A,2026-03-02,5.10\n"
)


def test_fx_pull_is_idempotent_and_incremental(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=ECB_CSV))
    assert non_eur_currencies() == ["BGN", "PLN", "RON"]
    pull_fx(settings, start=date(2026, 3, 1), transport=transport)
    revised = ECB_CSV.replace("4.30", "4.31") + (
        "EXR.D.PLN.EUR.SP00.A,D,PLN,EUR,SP00,A,2026-03-04,4.32\n"
    )
    pull_fx(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=revised)))
    glob = (settings.layer_dir("silver", "fx_daily") / "fx_daily.parquet").as_posix()
    rows = duckdb.sql(
        f"select currency, rate_date::varchar, units_per_eur from read_parquet('{glob}')"
        " order by 1, 2"
    ).fetchall()
    assert rows == [
        ("PLN", "2026-03-02", 4.25),
        ("PLN", "2026-03-03", 4.31),
        ("PLN", "2026-03-04", 4.32),
        ("RON", "2026-03-02", 5.1),
    ]
