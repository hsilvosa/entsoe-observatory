"""Bronze-load benchmark: one year (2025) of both ENTSO-E tables, parquet -> bronze parquet.

Each engine runs in its own subprocess; the parent samples the process tree (RSS incl. JVM
children) and system CPU every 0.2 s. All engines are capped at 4 threads.

Work per engine:
  1. read 2025 rows of day_ahead_prices + actual_load from the local snapshot
  2. add ingestion metadata (ingested_at, dataset, year, month)
  3. write bronze parquet (zstd) partitioned by dataset/year/month
  4. silver-style dedup: newest revision per (dataset, zone, ts, resolution, sequence) -> count
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path

import psutil

SRC = "D:/datasets/entsoe-transparency/data/processed"
OUT = Path(os.environ.get("BENCH_OUT", "C:/Users/Usuario/AppData/Local/hermes/cache/scratch/obs-phase0/bench_out"))
THREADS = 4
YEAR = 2025


def run_duckdb() -> dict:
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET threads={THREADS}; SET TimeZone='UTC'; SET memory_limit='4GB'")
    out = (OUT / "duckdb").as_posix()
    con.execute(f"""
      CREATE TEMP TABLE b AS
      SELECT 'day_ahead_prices' AS dataset, record_id, timestamp_utc, zone_key, eic_code, price AS value,
             currency, unit, resolution, coalesce(classification_position,0) AS seq, revision_number,
             source_file, now() AS ingested_at, year(timestamp_utc) AS year, month(timestamp_utc) AS month
      FROM read_parquet('{SRC}/day_ahead_prices/*.parquet') WHERE timestamp_utc >= '{YEAR}-01-01' AND timestamp_utc < '{YEAR+1}-01-01'
      UNION ALL BY NAME
      SELECT 'actual_load' AS dataset, record_id, timestamp_utc, zone_key, eic_code, load AS value, NULL::VARCHAR AS currency, unit, resolution, 0 AS seq,
             revision_number, source_file, now() AS ingested_at, year(timestamp_utc) AS year, month(timestamp_utc) AS month
      FROM read_parquet('{SRC}/actual_load/*.parquet') WHERE timestamp_utc >= '{YEAR}-01-01' AND timestamp_utc < '{YEAR+1}-01-01'
    """)
    rows = con.execute("SELECT count(*) FROM b").fetchone()[0]
    con.execute(f"COPY b TO '{out}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (dataset, year, month))")
    dedup = con.execute("""SELECT count(*) FROM (SELECT * FROM b QUALIFY row_number() OVER (
        PARTITION BY dataset, zone_key, timestamp_utc, resolution, seq ORDER BY revision_number DESC) = 1)""").fetchone()[0]
    return {"rows": rows, "dedup_rows": dedup}


def run_polars() -> dict:
    os.environ["POLARS_MAX_THREADS"] = str(THREADS)
    from datetime import datetime

    import polars as pl

    lo, hi = datetime(YEAR, 1, 1, tzinfo=UTC), datetime(YEAR + 1, 1, 1, tzinfo=UTC)
    common = ["record_id", "timestamp_utc", "zone_key", "eic_code", "unit", "resolution", "revision_number", "source_file"]
    p = (pl.scan_parquet(f"{SRC}/day_ahead_prices/*.parquet")
         .filter(pl.col("timestamp_utc").is_between(lo, hi, closed="left"))
         .select(*common, pl.col("price").alias("value"), "currency",
                 pl.col("classification_position").fill_null(0).alias("seq"),
                 pl.lit("day_ahead_prices").alias("dataset")))
    lo_ = (pl.scan_parquet(f"{SRC}/actual_load/*.parquet")
           .filter(pl.col("timestamp_utc").is_between(lo, hi, closed="left"))
           .select(*common, pl.col("load").alias("value"), pl.lit(None, pl.String).alias("currency"),
                   pl.lit(0, pl.Int32).alias("seq"), pl.lit("actual_load").alias("dataset")))
    b = (pl.concat([p, lo_]).with_columns(
            ingested_at=pl.lit(datetime.now(UTC)),
            year=pl.col("timestamp_utc").dt.year(), month=pl.col("timestamp_utc").dt.month())
         .collect())
    b.write_parquet(OUT / "polars", compression="zstd", partition_by=["dataset", "year", "month"])
    dedup = (b.sort("revision_number", descending=True)
              .unique(["dataset", "zone_key", "timestamp_utc", "resolution", "seq"], keep="first").height)
    return {"rows": b.height, "dedup_rows": dedup}


def run_spark() -> dict:
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ.pop("SPARK_HOME", None)  # use the pip pyspark 3.5.5 jars, not C:/Spark/spark-3.5.3
    from pyspark.sql import SparkSession, Window
    from pyspark.sql import functions as F

    spark = (SparkSession.builder.master(f"local[{THREADS}]").appName("bench")
             .config("spark.driver.memory", "4g")
             .config("spark.sql.shuffle.partitions", str(THREADS * 2))
             .config("spark.sql.session.timeZone", "UTC")
             .config("spark.sql.parquet.compression.codec", "zstd")
             .config("spark.ui.enabled", "false")
             .getOrCreate())
    spark.sparkContext.setLogLevel("ERROR")
    t0 = time.perf_counter()
    rng = (F.col("timestamp_utc") >= F.lit(f"{YEAR}-01-01").cast("timestamp")) & \
          (F.col("timestamp_utc") < F.lit(f"{YEAR+1}-01-01").cast("timestamp"))
    common = ["record_id", "timestamp_utc", "zone_key", "eic_code", "unit", "resolution", "revision_number", "source_file"]
    p = (spark.read.parquet(f"{SRC}/day_ahead_prices").where(rng)
         .select(*common, F.col("price").alias("value"), "currency",
                 F.coalesce("classification_position", F.lit(0)).alias("seq"),
                 F.lit("day_ahead_prices").alias("dataset")))
    lo_ = (spark.read.parquet(f"{SRC}/actual_load").where(rng)
           .select(*common, F.col("load").alias("value"), F.lit(None).cast("string").alias("currency"),
                   F.lit(0).alias("seq"), F.lit("actual_load").alias("dataset")))
    b = (p.unionByName(lo_).withColumn("ingested_at", F.current_timestamp())
         .withColumn("year", F.year("timestamp_utc")).withColumn("month", F.month("timestamp_utc")))
    b.write.mode("overwrite").partitionBy("dataset", "year", "month").parquet((OUT / "spark").as_posix())
    bronze = spark.read.parquet((OUT / "spark").as_posix())
    rows = bronze.count()
    w = Window.partitionBy("dataset", "zone_key", "timestamp_utc", "resolution", "seq").orderBy(F.desc("revision_number"))
    dedup = bronze.withColumn("rn", F.row_number().over(w)).where("rn = 1").count()
    work = time.perf_counter() - t0
    spark.stop()
    return {"rows": rows, "dedup_rows": dedup, "work_s_excl_jvm_start": round(work, 2)}


ENGINES = {"duckdb": run_duckdb, "polars": run_polars, "spark": run_spark}


def measure(engine: str) -> dict:
    target = OUT / engine
    shutil.rmtree(target, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    psutil.cpu_percent(None)
    t0 = time.perf_counter()
    proc = subprocess.Popen([sys.executable, __file__, "--child", engine], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ps = psutil.Process(proc.pid)
    peak, cpu, ctimes = 0, [], {}
    while proc.poll() is None:
        try:
            tree = [ps, *ps.children(recursive=True)]
            peak = max(peak, sum(p.memory_info().rss for p in tree if p.is_running()))
            for p in tree:
                c = p.cpu_times()
                ctimes[p.pid] = c.user + c.system
        except psutil.Error:
            pass
        cpu.append(psutil.cpu_percent(None))
        time.sleep(0.2)
    out, err = proc.communicate()
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        return {"engine": engine, "error": err[-2000:]}
    lines = [ln for ln in out.strip().splitlines() if ln.startswith("{")]
    if not lines:
        return {"engine": engine, "error": f"no result; stdout={out[-500:]!r} stderr={err[-1500:]!r}"}
    res = json.loads(lines[-1])
    size = sum(f.stat().st_size for f in target.rglob("*.parquet"))
    return {"engine": engine, "wall_s": round(wall, 2), "peak_rss_mb": round(peak / 2**20),
            "avg_cores_used": round(sum(ctimes.values()) / wall, 2),
            "cpu_pct_of_16_threads": round(100 * sum(ctimes.values()) / wall / psutil.cpu_count(), 1), "bronze_mb": round(size / 2**20, 1),
            "files": sum(1 for _ in target.rglob("*.parquet")), **res}


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(ENGINES[sys.argv[2]]()))
    else:
        engines = sys.argv[1:] or list(ENGINES)
        for e in engines:
            print(json.dumps(measure(e)), flush=True)
