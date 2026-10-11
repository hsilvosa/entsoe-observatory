# Phase 0 evidence

Scripts and raw outputs from the feasibility check run on 2026-10-11. They are kept for reproducibility; they are not part of the package.

| File | Purpose |
|---|---|
| `profile_data.py`, `profile_out.txt` | DuckDB profile of the local snapshot: rows per year and resolution, curve types, sequences, duplicates, DST days. |
| `a03_check.py` | Quantifies missing slots caused by unexpanded `curveType=A03` and confirms it on raw XML. |
| `bench_bronze.py`, `bench_results.jsonl` | Bronze-load benchmark for 2025 (DuckDB vs Polars vs Spark local, 4 threads). |
| `bench_xml.py` | Raw XML parse with A03 expansion for 2025. |
| `probes/test_phase0_probes.py` | Probe tests run against copies of the reusable `energy-predictor` modules. |

## Key results

- Machine: AMD Ryzen 7 5700X (16 threads), 32 GB RAM, Windows 11. All engines capped at 4 threads.
- Bronze load 2025 (1,727,832 rows, both tables, 2 runs each):
  - Polars: 1.9-2.4 s, 1.5-1.6 GB peak RSS, 2.3 cores.
  - DuckDB: 3.2 s, 1.35 GB peak RSS, 2.2-2.5 cores.
  - Spark local[4]: 20-22 s (15 s excluding JVM start), 1.9-2.2 GB peak RSS, 3.9-4.3 cores.
- Raw XML parse with A03 expansion, 2025 (699 files, 235 MB): 9.5 s, 65 MB, single process. 2,074,141 rows expanded, 291,236 of them filled by A03.
- Snapshot gaps (upper bound, includes real outages): prices 7.9 % of slots, load 6.2 %.
- Probe tests: 16 passed, 1 failed by design (`energy-predictor` parser does not expand A03).
