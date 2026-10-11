# ADR 0006: Data quality checks in SQL with severities

Status: accepted

## Decision

Checks run in DuckDB over silver and write `dq/run_id=<id>/check_results.parquet` and `completeness_daily.parquet`, plus `dq/latest.json`. Blocking: duplicate business key, unit consistency, currency not allowed for the zone. Warning: completeness, invalid values, value range, freshness, FX coverage. Information: resolution changes, currency changes. Blocking failures raise `DataQualityError` (exit code 1).

## Context

Completeness is measured in covered minutes against the DST-aware length of the local day, so it works with mixed resolutions and 23/25 hour days. The history has real gaps (outages, missing publications), so gaps are warnings and never block a run. Currency lists per zone include the local currency and EUR (PL, RO, BG changed currency during the history).
