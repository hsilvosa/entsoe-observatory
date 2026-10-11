"""Quantify A03 (compressed curve) gaps and confirm against raw XML."""
import glob
import xml.etree.ElementTree as ET

import duckdb

ROOT = "D:/datasets/entsoe-transparency/data"
con = duckdb.connect()
con.execute("SET threads=4; SET TimeZone='UTC'")

# Per zone/resolution/sequence: expected slots between first and last timestamp vs rows present.
for t in ("day_ahead_prices", "actual_load"):
    r = con.execute(f"""
      WITH s AS (
        SELECT zone_key, resolution, coalesce(classification_position,0) cp,
               min(timestamp_utc) a, max(timestamp_utc) b, count(*) n,
               CASE resolution WHEN 'PT15M' THEN 15 WHEN 'PT30M' THEN 30 ELSE 60 END m
        FROM read_parquet('{ROOT}/processed/{t}/*.parquet') GROUP BY 1,2,3)
      SELECT sum(n), sum(date_diff('minute', a, b)/m + 1)::BIGINT FROM s""").fetchone()
    print(t, "rows present", r[0], "slots in span", r[1], "missing", r[1]-r[0], f"({(r[1]-r[0])/r[1]:.1%})")

# Raw XML: ES prices, March 2024. Count points vs period length.
for f in sorted(glob.glob(f"{ROOT}/raw/day_ahead_prices/ES_202403*.xml"))[:1]:
    root = ET.parse(f).getroot()
    ns = {"n": root.tag.split("}")[0][1:]}
    periods = root.findall(".//n:Period", ns)
    pts = sum(len(p.findall("n:Point", ns)) for p in periods)
    print(f.split("/")[-1].split("\\")[-1], "curveType", root.find(".//n:curveType", ns).text,
          "periods", len(periods), "points", pts)
    p = next(p for p in periods if "2024-03-30T23" in p.find("n:timeInterval/n:start", ns).text)
    print(" period", p.find("n:timeInterval/n:start", ns).text, p.find("n:timeInterval/n:end", ns).text,
          "positions", [int(x.find("n:position", ns).text) for x in p.findall("n:Point", ns)])
