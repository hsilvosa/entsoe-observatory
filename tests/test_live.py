from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from xmlgen import DocSpec, PeriodSpec, SeriesSpec, render

from entsoe_observatory.client import EntsoeApiError, FetchResult
from entsoe_observatory.live import plan_window, pull_live
from entsoe_observatory.settings import Settings
from entsoe_observatory.silver import silver_as_of
from entsoe_observatory.zones import Zone, load_registry

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
START = "2026-03-09T23:00Z"
END = "2026-03-10T23:00Z"


class FakeFetcher:
    def __init__(self) -> None:
        self.payloads: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    def set_price_doc(self, zone: str, value: float, revision: int, created: str) -> None:
        period = PeriodSpec(START, END, "PT60M", {1: value})
        doc = DocSpec([SeriesSpec([period])], START, END, revision=revision, created=created,
                      mrid=f"doc-{revision}")
        self.payloads[("day_ahead_prices", zone)] = render(doc)

    def fetch(self, dataset: str, zone: Zone, start: datetime, end: datetime) -> FetchResult:
        self.calls.append((dataset, zone.key, start, end))
        body = self.payloads.get((dataset, zone.key))
        if body is None:
            return FetchResult(b"", "no_data", {}, "none", NOW)
        content = body.encode()
        return FetchResult(content, "data", {}, hashlib.sha256(content).hexdigest(), NOW)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, STORAGE_ROOT=tmp_path / "storage", THREADS=2)  # type: ignore[call-arg]


def silver_rows(settings: Settings) -> list[tuple[float, int]]:
    glob = (settings.layer_dir("silver", "day_ahead_prices") / "**" / "*.parquet").as_posix()
    return duckdb.sql(
        f"select value, revision_number from read_parquet('{glob}', hive_partitioning=false)"
        " order by timestamp_utc"
    ).fetchall()


def test_plan_window_uses_lookback_and_horizon() -> None:
    latest = datetime(2026, 3, 8, 22, 0, tzinfo=UTC)
    prices = plan_window("day_ahead_prices", latest, NOW, lookback_days=3, initial_days=7)
    assert prices.start == datetime(2026, 3, 5, tzinfo=UTC)
    assert prices.end == datetime(2026, 3, 12, tzinfo=UTC)
    load = plan_window("actual_load", None, NOW, lookback_days=3, initial_days=7)
    assert load.start == datetime(2026, 3, 3, tzinfo=UTC)
    assert load.end == datetime(2026, 3, 11, tzinfo=UTC)


def test_first_pull_builds_bronze_silver_and_landing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    fetcher = FakeFetcher()
    fetcher.set_price_doc("ES", 40.0, 1, "2026-03-09T11:00Z")
    summary = pull_live(settings, fetcher, zones=["ES", "PT"], datasets=["day_ahead_prices"],
                        now=NOW, run_id="r1")
    assert summary["rows"] == 24
    assert summary["no_data"] == 1  # PT has no payload
    assert silver_rows(settings) == [(40.0, 1)] * 24
    landing = list((settings.layer_dir("landing") / "live").rglob("*.xml"))
    assert len(landing) == 1
    manifest = (settings.layer_dir("landing") / "live_manifest.jsonl").read_text().splitlines()
    entry = json.loads(manifest[0])
    assert entry["zone_key"] == "ES" and len(entry["sha256"]) == 64
    assert "securityToken" not in json.dumps(entry)


def test_repeating_the_same_payload_is_idempotent(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    fetcher = FakeFetcher()
    fetcher.set_price_doc("ES", 40.0, 1, "2026-03-09T11:00Z")
    for run in ("r1", "r2"):
        pull_live(
            settings, fetcher, zones=["ES"], datasets=["day_ahead_prices"], now=NOW, run_id=run
        )
    assert silver_rows(settings) == [(40.0, 1)] * 24
    bronze = (settings.layer_dir("bronze", "day_ahead_prices") / "**" / "*.parquet").as_posix()
    count = duckdb.sql(
        f"select count(*) from read_parquet('{bronze}', hive_partitioning=false)"
    ).fetchone()
    assert count == (24,)


def test_newer_revision_wins_and_cutoff_restores_old_view(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    fetcher = FakeFetcher()
    fetcher.set_price_doc("ES", 40.0, 1, "2026-03-09T11:00Z")
    pull_live(settings, fetcher, zones=["ES"], datasets=["day_ahead_prices"], now=NOW, run_id="r1")
    fetcher.set_price_doc("ES", 55.0, 2, "2026-03-10T09:00Z")
    pull_live(settings, fetcher, zones=["ES"], datasets=["day_ahead_prices"], now=NOW, run_id="r2")
    assert silver_rows(settings) == [(55.0, 2)] * 24
    es = load_registry()["ES"]
    before = silver_as_of(
        settings, "day_ahead_prices", es, datetime(2026, 3, 10, 0, 0, tzinfo=UTC)
    )
    after = silver_as_of(settings, "day_ahead_prices", es, NOW)
    assert {r["value"] for r in before} == {40.0}
    assert {r["value"] for r in after} == {55.0}


class FlakyFetcher(FakeFetcher):
    """Fails the first call per zone with an API error, then behaves normally."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures
        self.seen: dict[str, int] = {}

    def fetch(self, dataset: str, zone: Zone, start: datetime, end: datetime) -> FetchResult:
        self.seen[zone.key] = self.seen.get(zone.key, 0) + 1
        if self.seen[zone.key] <= self.failures:
            raise EntsoeApiError("ENTSO-E request failed (HTTP 599)")
        return super().fetch(dataset, zone, start, end)


def test_failed_pulls_are_retried_once_after_a_pause(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    fetcher = FlakyFetcher(failures=1)
    fetcher.set_price_doc("ES", 50.0, 1, "2026-03-10T10:00:00Z")
    summary = pull_live(settings, fetcher, datasets=["day_ahead_prices"], zones=["ES"], now=NOW,
                        retry_pause=0)
    assert summary["errors"] == 0
    assert summary["rows"] > 0
    assert fetcher.seen["ES"] == 2


def test_pulls_that_keep_failing_are_reported(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    fetcher = FlakyFetcher(failures=5)
    summary = pull_live(settings, fetcher, datasets=["day_ahead_prices"], zones=["ES"], now=NOW,
                        retry_pause=0)
    assert summary["errors"] == 1
    assert fetcher.seen["ES"] == 2


def test_inactive_zone_is_skipped(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    pull_live(settings_for(tmp_path), fetcher, zones=["DE_AT_LU"], now=NOW, run_id="r1")
    assert fetcher.calls == []


def test_long_windows_are_split_below_one_year() -> None:
    from entsoe_observatory.live import Window, chunk_window

    window = Window(datetime(2024, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    chunks = chunk_window(window, 300)
    assert chunks[0].start == window.start and chunks[-1].end == window.end
    assert all((c.end - c.start).days <= 300 for c in chunks)
    assert all(a.end == b.start for a, b in zip(chunks, chunks[1:], strict=False))
