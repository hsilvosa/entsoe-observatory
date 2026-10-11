# entsoe-observatory

Reproducible, low-cost data platform for European electricity market data from the ENTSO-E Transparency Platform: day-ahead prices and actual total load for all configured bidding zones, 2015 to today.

Status: Phase 2 (live ingestion and data quality). The README grows with each phase.

## Layers

```
raw ENTSO-E XML -> bronze (parsed, A03 expanded, lineage) -> silver (typed, validated, deduplicated)
```

DuckDB is the primary engine. A Spark implementation of the silver logic is optional and covered by a parity test.

Besides prices and load, the platform ingests per-zone weather (Open-Meteo) and ECB FX rates, and checks the silver layer with SQL data quality checks.

## Commands

| Command | Purpose |
|---|---|
| `backfill` | Landing manifest, bronze and silver from the raw XML history. |
| `pull` | Incremental live pull from the ENTSO-E API (needs `ENTSOE_API_TOKEN` or `ENTSOE_ENV_FILE`). |
| `weather` | Open-Meteo weather per zone (`--start`, `--resume`). Stops cleanly on the API rate limit. |
| `fx` | ECB reference rates for the non-EUR currencies. |
| `dq` | Data quality checks; exits with code 1 on blocking failures. |
| `reconcile` | Compare silver with a legacy parquet snapshot. |
| `contracts` | Print the JSON schema of the event contract v2 (`schemas/grid-event-v2.json`). |

## Setup

```
uv sync --extra dev
cp .env.example .env   # fill STORAGE_ROOT and ENTSOE_RAW_DIR
uv run entsoe-observatory backfill
```

Windows with Spark: unset `SPARK_HOME` for the process, set `HADOOP_HOME` to a folder whose `bin` holds `winutils.exe` and `hadoop.dll` (Hadoop 3.3.6) and prepend that `bin` to `PATH`. Use a native Windows path for `PYSPARK_PYTHON`.

## Attribution

Parts of this code are adapted from the author's earlier projects:

- `energy-predictor` (time handling and DST test cases, client conventions).
- `entsoe-transparency` (XML parser structure, zone list, download manifest format).

## Further reading

- `docs/adr/` architecture decision records.
- `docs/phase0/` feasibility evidence.
