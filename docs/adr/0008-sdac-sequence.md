# ADR 0008: SDAC price sequence for AT and DE_LU

Status: accepted

## Decision

Gold prices use the series that the zone registry names in `sdac_sequence`: sequence 1 for AT and DE_LU, 0 (no classification) for every other zone. Sequence 2 stays in silver and is excluded from gold prices and from the completeness checks.

## Context

AT and DE_LU publish two day-ahead price series (`classification_position` 1 and 2). Evidence:

- Sequence 1 is hourly (PT60M) from 2018-09-30 to 2025-09-30 and quarter-hourly afterwards, the same calendar as the SDAC 15-minute go-live on 2025-10-01.
- Sequence 2 is quarter-hourly for the whole history, starting 2014-12-31 for AT. EPEX SPOT launched its 15-minute intraday call auction in Germany on 2014-12-09, held at 15:00 on the day before delivery. The start matches the first data day of the history.
- After 2025-10-01 both are quarter-hourly. Against neighbouring zones, sequence 1 stays closer in mean absolute difference (DE_LU vs NL 7.2 against 13.6 EUR/MWh; DE_LU vs DK_1 6.7 against 14.1; AT vs SI 6.3 against 15.3). Sequence 2 does not follow the coupled prices (2026-03-15 12:00Z, DE_LU: sequence 1 = 46.1, sequence 2 = 17.7; NL = 40.4).

Sequence 2 is therefore treated as the 15-minute intraday auction. This is an inference from the data and the EPEX launch date, not a statement of the ENTSO-E documentation. The registry field makes it easy to change.

DK_1 and DK_2 also show a second series (sequence 2, from January 2026, partial). Their SDAC series has no classification (sequence 0).
