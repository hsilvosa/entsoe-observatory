# Phase 2 results: live ingestion and data quality

Measured on 2026-10-11 (Ryzen 7 5700X, 32 GB, 4 threads). Times come from `scripts/measure_run.py` and the CLI.

## Live ENTSO-E pull

| Run | Wall | Result |
|---|---|---|
| Catch-up of all zones (history ended 2026-07/08) | 373 s | 570,559 rows |
| Second full pull (3 day lookback) | 147 s, 0.27 cores, 0.26 GB | 32,915 rows, 0 errors |
| Same payload again (ES prices) | 3 s | 384 rows re-read, silver unchanged (130,415 unique keys) |

The pull is bound by the API rate limit (3 requests per second, about 100 requests). Zones without data for a dataset (`no_data`): prices BA, CY, GB, LU, MT, TR; load GB, IE_SEM, MT, TR. Prices reach D+1 (2026-10-12 23:45 local) once the day-ahead auction is published.

Found while running: `DE_AT_LU` (closed in 2018) made the API answer HTTP 400. Closed zones are now skipped, windows are split below one year, and one failing zone no longer stops the run.

## Weather and FX

- ECB FX: 2 s, 8,862 rows. RON and PLN reach 2026-10-09; BGN stops at 2025-12-31 (euro adoption).
- Open-Meteo: ES full history 2015-2026 (5 points) took 19 s. The free tier stops the full backfill with an hourly limit: 26 of 51 zones were loaded before HTTP 429 "Hourly API request limit exceeded". `weather --resume` continues where it stopped.

## Data quality (764 checks, 118 s, 1.8 cores, 1.8 GB)

Result: 0 blocking failures, 57 warnings.

| Check | Severity | Failing series |
|---|---|---|
| duplicate business key, unit, currency | blocking | 0 |
| completeness (days with missing minutes) | warning | prices 16 of 45 zones, load 35 of 48 |
| freshness | warning | prices 3, load 3 |
| FX coverage, value range, invalid values | warning | 0 |
| resolution changes, currency changes | info | reported |

Findings:

- Currency changes during the history: BG BGN until 2022-01-22 then EUR, RO RON until 2021-06-18 then EUR, PL PLN for 2016-01-24 to 2019-11-19 and EUR otherwise. The zone registry now allows the local currency and EUR.
- Largest real gaps: PL prices 697 days without any price, DK_1 189 and DK_2 71 days (January 2026), AT 93. Load: AL 2,324 days, CY 1,331, IE_SEM 876 (no load since 2025-10).
- The first and last local day of each series are not reported as gaps because request windows start or end mid-day.
- Freshness: IE_SEM load is 8,479 hours old; BG and MK load lag 10 to 20 hours.
