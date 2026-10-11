# ADR 0003: Bronze source: raw ENTSO-E XML

Status: accepted

## Decision

Bronze is built from the raw XML documents, not from the normalized Parquet snapshot or the Hugging Face dataset.

## Context

The snapshot lost the period boundaries that are needed to expand compressed curves (ADR 0004), so it cannot be repaired. XML files are not copied: a landing manifest records path, size and sha256 of each file.
