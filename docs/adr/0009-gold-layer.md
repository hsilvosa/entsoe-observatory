# ADR 0009: Gold layer, contracts and EUR conversion

Status: accepted

## Decision

Gold has nine tables (`price_native`, `load_native`, `price_hourly`, `load_hourly`, `zone_daily`, `cross_zone_spread_hourly`, `weather_hourly`, `dq_scorecard`, `dq_scorecard_summary`) written as zstd Parquet under `STORAGE_ROOT/gold/<table>/`. Each table has a JSON contract in `contracts/gold/` with a semantic version, grain, partition columns, typed columns with nullability and row constraints.

- The contract drives the final projection of every build and is enforced on the written files before the table directory is swapped in: schema, nullability, grain uniqueness and constraints. A violation aborts the build and the previous table stays in place.
- A change of columns, types or constraints needs a new contract version.

## EUR conversion

`price_eur = price_local / units_per_eur` with the ECB reference rate of the delivery local date. The ECB publishes on working days, so the latest rate on or before the date is used, at most 5 days old. Older or missing rates give `fx_status = missing_rate` and a null `price_eur`; EUR prices are `native`. The rate and its date are stored in the row.

## Harmonised hourly grid

Hourly tables aggregate the finest resolution present in each hour by mean and publish `source_resolution`, `slots_present` and `slots_expected`. Hourly slots pass through; nothing is upsampled. Spreads between neighbouring zones use only hours where both zones have all expected slots.

## Consequences

Gold is rebuilt completely on every run (about 100 s for the full history). Incremental gold builds are an optimisation for later. Scorecard tables need a data quality run first.
