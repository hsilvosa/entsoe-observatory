# ADR 0011: Scheduled orchestration on GitHub Actions

Status: accepted

## Decision

Three scheduled workflows run the pipeline on GitHub-hosted runners (free for public repositories):

| Workflow | Schedule (UTC) | Steps |
|---|---|---|
| `update-load.yml` | hourly, minute 17 | pull load, push state |
| `update-prices.yml` | 11:25 and 13:25 | pull prices, FX, weather, DQ, gold, publish to R2, dashboard artifact |
| `weekly-dq.yml` | Monday 05:40 | DQ, gold, publish to R2, DQ report artifact |

- **Single writer:** every workflow uses the concurrency group `pipeline-writer` without cancelling, so runs queue instead of overlapping.
- **State:** runners are empty, so each run restores bronze, silver, landing and dq from R2 (`state-pull`) and uploads the changed files at the end (`state-push`, keyed by sha256 in `_state/manifest.json`). Gold is rebuilt from silver and is not part of the state. Replaced partitions leave orphan objects in the bucket; they are harmless and can be removed by hand.
- **Prices cron:** the day-ahead results appear around 13:00 CET, which is 11:00 or 12:00 UTC depending on summer time. Two runs cover both.
- **Alerts:** a failing step opens a GitHub issue labelled `pipeline-failure`, or comments on the open one, through `.github/actions/alert`. No paid service is involved.
- **Freshness:** thresholds live in the DQ checks: load must end less than 3 hours ago, prices must reach the day after the publication hour (14:00 CET) with one day of tolerance. A stale series is a warning in the weekly report; blocking checks (duplicates, units, currencies) fail the run and raise an issue.

## Consequences

Each run downloads the state (about 650 MB, no egress cost on R2). Secrets: `ENTSOE_API_TOKEN`, `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Scheduled workflows are disabled by GitHub after 60 days without repository activity.
