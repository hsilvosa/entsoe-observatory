# entsoe-observatory

Reproducible, low-cost data platform for European electricity market data from the ENTSO-E Transparency Platform: day-ahead prices and actual total load for all configured bidding zones, 2015 to today.

Status: Phase 1 (bronze and silver for the history). The README is a skeleton and grows with each phase.

## Layers

```
raw ENTSO-E XML -> bronze (parsed, A03 expanded, lineage) -> silver (typed, validated, deduplicated)
```

DuckDB is the primary engine. A Spark implementation of the silver logic is optional and covered by a parity test.

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
