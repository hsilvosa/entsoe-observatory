# Phase 1 results: bronze and silver for the history

Run on 2026-10-11 (Ryzen 7 5700X, 32 GB, DuckDB threads capped at 4). Command: `entsoe-observatory backfill` measured with `scripts/measure_run.py`.

| Stage | Wall time | Output |
|---|---|---|
| Landing manifest (6,183 XML files, sha256) | about 3 s (warm cache) | `landing/landing_manifest.json` |
| Bronze (parse, A03 expansion, partitioned parquet) | about 270 s with landing | 13,912,867 rows, 314 MB |
| Silver (DuckDB) | 172 s | 13,722,341 rows, 216 MB |
| Whole run | 445 s, 699 CPU s (1.57 cores on average), peak RSS 5.8 GB (process tree) | |

Bronze parsing is single process. The peak RSS comes from holding all rows of one (dataset, zone) as Arrow tables before the write.

## Counts

- A03-filled rows in bronze: 618,514. In silver: 604,285.
- Duplicates dropped on the business key: 190,526 (monthly files overlap by one local day). Out-of-window rows: 0. Invalid rows: 0 (no negative load in the history).
- Currencies and units: EUR 5,718,517, RON 56,519, BGN 45,433, PLN 23,895 price rows; load only `MW`.
- Price range -500 to 6,101.78; load range 0 to 94,492 MW.

## Reconciliation with the old snapshot

| Dataset | Old rows | Silver rows | Added | Old keys missing in silver |
|---|---|---|---|---|
| day_ahead_prices | 5,423,638 | 5,844,364 | 420,726 | 0 |
| actual_load | 7,690,918 | 7,877,977 | 187,059 | 0 |

DST days on real data (ES, after expansion): load 92 / 96 / 100 quarter hours on 2025-03-30 / 2025-06-01 / 2025-10-26 (the old snapshot gave 98 on the last); prices 23 / 25 hours on 2024-03-31 / 2024-10-27.
