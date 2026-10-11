"""Phase 0 data profiling of the local ENTSO-E snapshot (read-only)."""
import duckdb

ROOT = "D:/datasets/entsoe-transparency/data/processed"
con = duckdb.connect()
con.execute("SET threads=4")
con.execute("SET TimeZone='UTC'")

for t, v in (("day_ahead_prices", "price"), ("actual_load", "load")):
    src = f"read_parquet('{ROOT}/{t}/*.parquet')"
    con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM {src}")
    print(f"\n===== {t}")
    q = lambda s: con.execute(s).fetchall()
    print("rows/year/resolution:")
    for r in q(f"SELECT year(timestamp_utc) y, resolution, count(*) FROM {t} GROUP BY ALL ORDER BY 1,2"):
        print("  ", r)
    print("curve_type:", q(f"SELECT curve_type, count(*) FROM {t} GROUP BY 1"))
    print("business/contract/auction:", q(f"SELECT business_type, contract_type, auction_type, count(*) FROM {t} GROUP BY ALL ORDER BY 4 DESC LIMIT 10"))
    print("revision_number:", q(f"SELECT revision_number, count(*) FROM {t} GROUP BY 1 ORDER BY 1"))
    print("classification_position:", q(f"SELECT classification_position, count(*) FROM {t} GROUP BY 1 ORDER BY 1 LIMIT 10"))
    # Same zone+timestamp with >1 row (mixed resolutions or multiple series).
    print("dup (zone,ts) groups by resolution set:", q(f"""
        SELECT res_set, count(*) FROM (
          SELECT zone_key, timestamp_utc, string_agg(DISTINCT resolution, ',' ORDER BY resolution) res_set, count(*) n
          FROM {t} GROUP BY 1,2 HAVING n>1) GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""))
    print("dup (zone,ts) with conflicting values:", q(f"""
        SELECT count(*) FROM (SELECT zone_key, timestamp_utc, count(DISTINCT {v}) d
        FROM {t} GROUP BY 1,2 HAVING d>1)"""))
    # Zones that switched 60 -> 15 min, and when.
    print("first PT15M per zone (zones with both 60 and 15):")
    for r in q(f"""
        SELECT zone_key, min(timestamp_utc) FILTER (WHERE resolution='PT15M') f15,
               max(timestamp_utc) FILTER (WHERE resolution='PT60M') l60
        FROM {t} GROUP BY 1
        HAVING f15 IS NOT NULL AND l60 IS NOT NULL ORDER BY f15"""):
        print("  ", r)
    # DST: rows per local day for ES on transition days vs normal days.
    print("ES rows per Europe/Madrid local day on DST days (and a normal day):")
    for r in q(f"""
        SELECT CAST(timezone('Europe/Madrid', timestamp_utc) AS DATE) d, resolution, count(*)
        FROM {t} WHERE zone_key='ES'
          AND CAST(timezone('Europe/Madrid', timestamp_utc) AS DATE) IN
          ('2024-03-31','2024-10-27','2024-06-12','2025-03-30','2025-10-26','2026-03-29')
        GROUP BY ALL ORDER BY 1,2"""):
        print("  ", r)
    # Gap check: per zone, compare expected slots vs rows using native resolution (ES 2025).
    print("ES 2025 rows by resolution vs expected:", q(f"""
        SELECT resolution, count(*) FROM {t} WHERE zone_key='ES' AND year(timestamp_utc)=2025 GROUP BY 1"""),
          "expected 60m=8760, 15m=35040")
    print("value stats by zone (top 5 rows):", q(f"SELECT zone_key, min({v}), max({v}), count(*) FROM {t} GROUP BY 1 ORDER BY 4 DESC LIMIT 5"))
    print("2025 rows / zones:", q(f"SELECT count(*), count(DISTINCT zone_key) FROM {t} WHERE year(timestamp_utc)=2025"))
