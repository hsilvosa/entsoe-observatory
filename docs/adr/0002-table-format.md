# ADR 0002: Table format: partitioned Parquet with partition swap

Status: accepted

## Decision

Tables are Hive-partitioned Parquet (zstd) under `STORAGE_ROOT/<layer>/<dataset>/zone=<key>/year=<yyyy>/month=<mm>/`. A write builds a new zone directory next to the old one and swaps it, so a run is idempotent. Each run writes a manifest with the run id, input checksums, row counts and code version.

## Consequences

Delta Lake is a possible extension. Version 1 has a single writer on object storage, where plain Parquet avoids commit locking.
