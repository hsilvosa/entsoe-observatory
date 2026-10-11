"""Dataset card (README.md) of the published gold dataset, generated from contracts and runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb

from .gold import TABLE_ORDER, load_contract
from .settings import Settings

REPO_URL = "https://github.com/hsilvosa/entsoe-observatory"

LIMITATIONS = [
    "Gaps are real: some zones have days without any published price or load (for example PL "
    "prices before 2017, AL load, CY load, IE_SEM load since 2025-10). Check `dq_scorecard`.",
    "Prices of AT and DE_LU use the SDAC series (sequence 1). The second series published for "
    "these zones is excluded; ADR 0008 in the repository explains the choice and its evidence.",
    "Weather covers only the zones listed in `weather_hourly`; the series is a population-weighted "
    "mean over a few points per zone, not a measurement.",
    "EUR conversion of RON, PLN and BGN prices uses the latest ECB rate up to 5 days old. Rows "
    "without such a rate keep a null `price_eur`.",
    "`cross_zone_spread_hourly` is based on a hand-maintained list of neighbouring zones "
    "(`configs/neighbours.json`).",
]


def _latest_manifest(settings: Settings, stage: str) -> dict[str, Any] | None:
    files = sorted(settings.layer_dir("manifests").glob(f"*_{stage}.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def build_card(settings: Settings) -> str:
    gold = _latest_manifest(settings, "gold")
    if gold is None:
        raise FileNotFoundError("no gold run manifest: run `gold` first")
    con = duckdb.connect()
    # Row counts come from the files, so a partial rebuild still gives a complete card.
    rows: dict[str, dict[str, Any]] = {}
    for name in TABLE_ORDER:
        directory = settings.layer_dir("gold", name)
        if directory.exists():
            pattern = (directory / "**" / "*.parquet").as_posix()
            counted = con.execute(
                f"SELECT count(*) FROM read_parquet('{pattern}', hive_partitioning=false)"
            ).fetchone()
            rows[name] = {"rows": counted[0] if counted else 0}
    zone_daily = (settings.layer_dir("gold", "zone_daily") / "**" / "*.parquet").as_posix()
    span = con.execute(
        f"SELECT min(local_date), max(local_date), count(DISTINCT zone_key) "
        f"FROM read_parquet('{zone_daily}', hive_partitioning=false)"
    ).fetchone()
    assert span is not None
    first, last, zones = span
    weather_zones = 0
    if "weather_hourly" in rows:
        pattern = (settings.layer_dir("gold", "weather_hourly") / "**" / "*.parquet").as_posix()
        row = con.execute(
            f"SELECT count(DISTINCT zone_key) FROM read_parquet('{pattern}', "
            "hive_partitioning=false)"
        ).fetchone()
        weather_zones = row[0] if row else 0
    dq_file = settings.layer_dir("dq") / "latest.json"
    dq = json.loads(dq_file.read_text(encoding="utf-8")) if dq_file.exists() else {}

    configs = "\n".join(
        f"  - config_name: {name}\n    data_files:\n      - split: train\n"
        f"        path: data/{name}/**/*.parquet"
        for name in TABLE_ORDER
        if name in rows
    )
    front = (
        "---\n"
        "pretty_name: ENTSO-E observatory (gold)\n"
        "license: other\n"
        "license_name: entsoe-reuse-with-attribution\n"
        "language:\n  - en\n"
        "tags:\n  - energy\n  - electricity\n  - time-series\n  - day-ahead-prices\n  - europe\n"
        "size_categories:\n  - 10M<n<100M\n"
        f"configs:\n{configs}\n"
        "---\n"
    )
    table_lines = []
    for name in TABLE_ORDER:
        if name not in rows:
            continue
        contract = load_contract(name)
        info = rows[name]
        columns = "\n".join(
            f"| `{c['name']}` | {c['type']} | {'yes' if c['nullable'] else 'no'} | "
            f"{c['description']} |"
            for c in contract.columns
        )
        table_lines.append(
            f"### `{name}` (contract {contract.version}, {info['rows']:,} rows)\n\n"
            f"{contract.description}\n\nGrain: `{', '.join(contract.grain)}`.\n\n"
            "| Column | Type | Nullable | Description |\n|---|---|---|---|\n"
            f"{columns}\n"
        )
    body = f"""
# ENTSO-E observatory: gold tables

Day-ahead prices and actual total load of European bidding zones from the ENTSO-E
Transparency Platform, with per-zone weather, ECB exchange rates, daily statistics, price
spreads between neighbouring zones and a data quality scorecard. Local days from {first} to
{last}, {zones} zones. Built and published by the open source project
[entsoe-observatory]({REPO_URL}).

Generated on {datetime.now(UTC):%Y-%m-%d} from gold run `{gold.get('run_id')}` and data quality
run `{dq.get('run_id', 'n/a')}` ({dq.get('blocking_failures', 'n/a')} blocking failures,
{dq.get('warning_failures', 'n/a')} warnings).

## Tables

All tables are Parquet (zstd). Every table has a versioned JSON contract in `contracts/`; the
build enforces schema, nullability, grain uniqueness and row constraints before publishing.

{chr(10).join(table_lines)}
## How the data is built

- **Source:** raw ENTSO-E XML documents (day-ahead prices A44, actual total load A65).
- **A03 curves:** ENTSO-E compresses series with `curveType=A03`: an omitted position repeats
  the previous value until the end of the period. The build expands them and marks the
  generated slots with `is_a03_filled`. Without it about 8 % of price slots and 6 % of load
  slots would be missing.
- **Native resolution:** PT60M, PT30M and PT15M coexist (the 15-minute day-ahead market started on
  2025-10-01). Native tables keep the source resolution. Hourly tables average the finest
  resolution present in each hour and publish `slots_present` and `slots_expected`. Nothing is
  upsampled.
- **Time:** timestamps are UTC; local time follows each zone's IANA timezone, so local days have
  23, 24 or 25 hours.
- **Revisions:** the newest revision of a document wins; late revisions can be cut off at a
  snapshot time.
- **EUR conversion:** `price_eur = price_local / units_per_eur` with the ECB reference rate of
  the delivery date (latest rate up to 5 days old).

## Known limitations

{chr(10).join('- ' + item for item in LIMITATIONS)}
- Weather is available for {weather_zones} of {zones} zones in this release.

## Sources and licence

- ENTSO-E Transparency Platform (https://transparency.entsoe.eu). Re-use requires mentioning the
  ENTSO-E Transparency Platform as the source and must not suggest endorsement by ENTSO-E. The
  terms of use and the list of data open for free re-use are published by ENTSO-E; check them
  before redistributing.
- Open-Meteo (https://open-meteo.com), CC BY 4.0, weather data derived from national weather
  services and reanalysis.
- European Central Bank euro foreign exchange reference rates, free re-use with attribution.

The tables in this dataset are derived works. They carry no warranty; use them at your own
responsibility. The code that builds them is in the project repository.

## Citation

```
entsoe-observatory, gold tables built from ENTSO-E Transparency Platform data, {REPO_URL}
```
"""
    return front + body
