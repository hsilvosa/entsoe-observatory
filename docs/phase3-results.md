# Phase 3 results: gold, contracts, dashboard and publication

Measured on 2026-10-11 (Ryzen 7 5700X, 32 GB, 4 threads).

## Gold build (`entsoe-observatory gold`)

Wall time 103 s, 2.5 cores on average, 2.7 GB peak RSS. All nine tables passed their contracts (schema, nullability, grain uniqueness and row constraints).

| Table | Rows | Size |
|---|---|---|
| price_native | 5,425,040 | 71.4 MB |
| load_native | 8,134,301 | 81.8 MB |
| price_hourly | 4,145,957 | 57.4 MB |
| load_hourly | 4,657,565 | 51.2 MB |
| zone_daily | 194,988 | 4.4 MB |
| cross_zone_spread_hourly | 6,957,668 | 63.0 MB |
| weather_hourly | 3,717,192 (36 of 51 zones) | 41.7 MB |
| dq_scorecard | 378,753 | 0.2 MB |
| dq_scorecard_summary | 93 | under 0.1 MB |

`price_native` has 700k fewer rows than silver because the second AT and DE_LU series is left out (ADR 0008). EUR conversion: 5.30M rows native EUR, 125,847 rows converted (BGN 45,433, RON 56,519, PLN 23,895), none without a rate.

## Dashboard (`entsoe-observatory dashboard`)

1.1 s, 53 JSON files, 9.4 MB before compression. Checked in Edge through Playwright: coverage table, completeness heat map, explorer, negative-price ranking and spreads render without console errors.

## Publication

`publish-r2 --dry-run` and `publish-hf --dry-run` plan 2,471 files, 359.5 MB (gold, contracts and the generated dataset card). Real uploads need R2 and Hugging Face credentials and were not run. Both use fakes in the tests: second run uploads nothing, changed files only, prune, dry run, no secret in any manifest.

## Findings

- AT and DE_LU: sequence 1 is SDAC; sequence 2 matches the 15-minute intraday auction that EPEX SPOT started in Germany on 2014-12-09 (ADR 0008). DK_1 and DK_2 have a partial second series since January 2026; their SDAC series has no classification. Completeness now looks only at the SDAC series, which removed the false gaps for DK and AT.
- Negative-price hours in 2025 (hourly grid): SE_2 681, NL 584, DE_LU 576, ES 556, SE_1 520, BE 519, FR 513.
- Largest mean absolute spreads in the last 12 months: FR-IT_NORD 62 EUR/MWh, NO_3-NO_4 46, LT-PL 46, FR-CH 45.
- Open-Meteo free tier: 36 of 51 zones loaded when the daily limit stopped the backfill. `weather --resume` continues after the reset.
