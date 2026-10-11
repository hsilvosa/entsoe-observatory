# ADR 0004: Expansion of curveType A03

Status: accepted

## Decision

Every series uses `curveType=A03`: a position that is omitted repeats the previous value until the end of the Period. The parser expands each Period up to its end and marks generated rows with `is_a03_filled`.

## Context

Not expanding loses about 7.9 % of price slots and 6.2 % of load slots. Filled rows are kept, flagged, and lose to real rows when the same key appears twice. Resolution is never changed in silver: PT15M, PT30M and PT60M coexist. The business key is (dataset, zone, timestamp_utc, resolution, sequence).
