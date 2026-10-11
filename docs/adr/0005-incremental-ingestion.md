# ADR 0005: Incremental ingestion with lookback and revision cutoff

Status: accepted

## Decision

Live pulls read a window from `newest silver timestamp - lookback` (default 3 days) to the publication horizon (D+2 for prices, D+1 for load) and split windows longer than 300 days. Each payload is stored in landing with its sha256, merged into the affected bronze months and silver is rebuilt for those months only. A payload that is already stored (same sha256) replaces its own rows, so re-running is idempotent.

## Consequences

Silver ranks duplicates by revision number, then document creation time, then real over A03-filled. `silver_as_of(zone, instant)` ignores documents created after the instant, so a snapshot never sees a late revision. Closed zones (`valid_to` in the past) are not pulled. One failing zone is recorded as an error and does not stop the others; the CLI exits with code 1 at the end.
