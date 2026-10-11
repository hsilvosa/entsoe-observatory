"""Print a summary of the newest data quality run (reads STORAGE_ROOT/dq)."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

root = Path(os.environ.get("STORAGE_ROOT", "./storage")) / "dq"
run = sorted(root.glob("run_id=*"))[-1]
f = (run / "check_results.parquet").as_posix()
print(run.name)
queries = {
    "by check": f"select check_name, severity, dataset, count(*) filter (status='fail') failing, "
    f"count(*) zones from read_parquet('{f}') group by all order by 1, 3",
    "worst price completeness": f"select zone_key, failed_count, total_count, detail "
    f"from read_parquet('{f}') where check_name='completeness' and dataset='day_ahead_prices' "
    "and status='fail' order by failed_count desc limit 8",
    "worst load completeness": f"select zone_key, failed_count, total_count, detail "
    f"from read_parquet('{f}') where check_name='completeness' and dataset='actual_load' "
    "and status='fail' order by failed_count desc limit 8",
    "other failures": f"select check_name, zone_key, dataset, detail from read_parquet('{f}') "
    "where check_name in ('freshness','fx_coverage','value_range','invalid_values') "
    "and status='fail' order by 1, 2 limit 20",
    "currency changes": f"select zone_key, detail from read_parquet('{f}') "
    "where check_name='currency_changes' and failed_count>0",
}
for title, sql in queries.items():
    print("==", title)
    for row in duckdb.sql(sql).fetchall():
        print(row)
