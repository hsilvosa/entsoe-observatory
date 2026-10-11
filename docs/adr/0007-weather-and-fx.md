# ADR 0007: Weather and FX sources

Status: accepted

## Decision

Weather comes from Open-Meteo: the ERA5 archive API up to 7 days ago and the forecast API for the recent days; the archive wins when both cover an hour. Each zone has 1-5 population-weighted points (`configs/weather_points.json`). Silver is the weighted zone mean per hour with weights renormalised over non-null points. Wind speed is requested in m/s explicitly because the API default is km/h. FX comes from the ECB SDMX API (units of currency per EUR) for the non-EUR currencies of the registry.

## Consequences

The ECB publishes working days only and stopped BGN after 2025-12-31 (euro adoption). The fill rule for other days belongs to the Phase 3 conversion method; the `fx_coverage` check allows 5 days back.
