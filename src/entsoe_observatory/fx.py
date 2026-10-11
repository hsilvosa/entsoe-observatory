"""ECB euro foreign exchange reference rates (units of currency per 1 EUR).

Used in Phase 3 to express non-EUR prices (RON, BGN, PLN) in EUR. The ECB publishes on
working days only; the fill rule for other days belongs to the conversion method.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .bronze import new_run_id, write_run_manifest
from .settings import Settings
from .zones import load_registry

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{currencies}.EUR.SP00.A"
FX_SCHEMA = pa.schema(
    [
        ("currency", pa.string()),
        ("rate_date", pa.date32()),
        ("units_per_eur", pa.float64()),
        ("source_sha256", pa.string()),
        ("run_id", pa.string()),
        ("ingested_at_utc", pa.timestamp("us", tz="UTC")),
    ]
)


def non_eur_currencies(registry_path: Path | None = None) -> list[str]:
    registry = load_registry(registry_path)
    return sorted({c for zone in registry.values() for c in zone.currencies} - {"EUR"})


def parse_ecb_csv(
    content: bytes, *, sha256: str, run_id: str, ingested_at: datetime
) -> pa.Table:
    cols: dict[str, list[Any]] = {name: [] for name in FX_SCHEMA.names}
    for row in csv.DictReader(io.StringIO(content.decode("utf-8-sig"))):
        value = row.get("OBS_VALUE")
        if not value:
            continue
        cols["currency"].append(row["CURRENCY"])
        cols["rate_date"].append(date.fromisoformat(row["TIME_PERIOD"]))
        cols["units_per_eur"].append(float(value))
        cols["source_sha256"].append(sha256)
        cols["run_id"].append(run_id)
        cols["ingested_at_utc"].append(ingested_at)
    return pa.table(cols, schema=FX_SCHEMA)


def fetch_ecb(
    currencies: list[str],
    start: date,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    url = ECB_URL.format(currencies="+".join(currencies))
    with httpx.Client(timeout=60, transport=transport) as http:
        response = http.get(
            url, params={"startPeriod": start.isoformat(), "format": "csvdata"}
        )
    if response.status_code >= 400:
        raise RuntimeError(f"ECB request failed (HTTP {response.status_code})")
    return response.content


SILVER_SQL = """
SELECT currency, rate_date, units_per_eur, units_per_eur > 0 AS is_valid, source_sha256
FROM (
    SELECT *, row_number() OVER (
        PARTITION BY currency, rate_date ORDER BY ingested_at_utc DESC
    ) AS rn FROM bronze_fx
) WHERE rn = 1 ORDER BY currency, rate_date
"""


def pull_fx(
    settings: Settings,
    *,
    start: date | None = None,
    currencies: list[str] | None = None,
    run_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    ingested_at = datetime.now(UTC)
    currencies = currencies or non_eur_currencies(registry_path)
    bronze_path = settings.layer_dir("bronze", "fx") / "ecb_rates.parquet"
    existing: pa.Table | None = pq.read_table(bronze_path) if bronze_path.exists() else None
    if start is None:
        start = date(2015, 1, 1)
        if existing is not None and existing.num_rows:
            latest = max(d for d in existing["rate_date"].to_pylist())
            start = latest - timedelta(days=10)  # lookback for ECB corrections
    assert start is not None
    content = fetch_ecb(currencies, start, transport=transport)
    sha = hashlib.sha256(content).hexdigest()
    landing = settings.layer_dir("landing") / "fx" / f"ecb_{start:%Y%m%d}_{sha[:12]}.csv"
    landing.parent.mkdir(parents=True, exist_ok=True)
    landing.write_bytes(content)
    fresh = parse_ecb_csv(content, sha256=sha, run_id=run_id, ingested_at=ingested_at)
    if fresh.num_rows == 0:
        raise RuntimeError("ECB returned no observations")
    table = fresh
    if existing is not None and existing.num_rows:
        keep = existing.filter(pc.less(existing["rate_date"], pa.scalar(start, pa.date32())))
        table = pa.concat_tables([keep.cast(FX_SCHEMA), fresh])
    bronze_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = bronze_path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(bronze_path)

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(
        f"CREATE TEMP TABLE bronze_fx AS SELECT * FROM read_parquet('{bronze_path.as_posix()}')"
    )
    silver_dir = settings.layer_dir("silver", "fx_daily")
    silver_dir.mkdir(parents=True, exist_ok=True)
    silver_path = silver_dir / "fx_daily.parquet"
    con.execute(
        f"COPY ({SILVER_SQL}) TO '{silver_path.as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
    )
    count = con.execute(
        f"SELECT count(*) FROM read_parquet('{silver_path.as_posix()}')"
    ).fetchone()
    summary = {
        "currencies": currencies,
        "start": start.isoformat(),
        "fetched_rows": fresh.num_rows,
        "silver_rows": count[0] if count else 0,
        "sha256": sha,
    }
    write_run_manifest(settings, run_id, "fx", summary)
    return summary
