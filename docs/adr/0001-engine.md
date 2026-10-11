# ADR 0001: Engine: DuckDB primary, Spark optional

Status: accepted

## Decision

DuckDB is the primary engine. A Spark implementation of the silver transformations is kept as an optional path and must give identical results on a sample (parity test).

## Context

Phase 0 benchmark, 2025 bronze load (1.73M rows, 4 threads): DuckDB 3.2 s / 1.35 GB, Polars 1.9-2.4 s / 1.5 GB, Spark local 20 s / 1.9-2.2 GB. The full history is about 1 GB of Parquet, far below the size where Spark pays off, and DuckDB runs for free on a CI runner.
