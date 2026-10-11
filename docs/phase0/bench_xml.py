"""Raw XML -> rows benchmark for one year (2025), A03-aware, single process (stdlib ElementTree)."""
from __future__ import annotations

import glob
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import psutil

RAW = "D:/datasets/entsoe-transparency/data/raw"


def dur(v: str) -> timedelta:
    h, m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", v).groups()
    return timedelta(hours=int(h or 0), minutes=int(m or 0))


def parse(path: str, value_tag: str):
    root = ET.parse(path).getroot()
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        return
    ns = root.tag.split("}")[0] + "}"
    for ts in root.iter(ns + "TimeSeries"):
        curve = ts.findtext(ns + "curveType")
        for p in ts.iter(ns + "Period"):
            start = datetime.fromisoformat(p.findtext(f"{ns}timeInterval/{ns}start").replace("Z", "+00:00"))
            end = datetime.fromisoformat(p.findtext(f"{ns}timeInterval/{ns}end").replace("Z", "+00:00"))
            step = dur(p.findtext(ns + "resolution"))
            n = int((end - start) / step)
            pts = {int(pt.findtext(ns + "position")): float(pt.findtext(ns + value_tag)) for pt in p.iter(ns + "Point")}
            last = None
            for pos in range(1, n + 1):
                if pos in pts:
                    last = pts[pos]
                elif curve != "A03" or last is None:
                    continue
                yield start + step * (pos - 1), last, pos in pts


files = [(f, "price.amount") for f in glob.glob(f"{RAW}/day_ahead_prices/*_2025*.xml")]
files += [(f, "quantity") for f in glob.glob(f"{RAW}/actual_load/*_20250101_*.xml")]
size = sum(os.path.getsize(f) for f, _ in files)
t0 = time.perf_counter()
rows = filled = 0
for f, tag in files:
    for _, _, present in parse(f, tag):
        rows += 1
        filled += not present
wall = time.perf_counter() - t0
print({"files": len(files), "xml_mb": round(size / 2**20, 1), "rows_expanded": rows, "rows_filled_by_A03": filled,
       "wall_s": round(wall, 1), "peak_rss_mb": round(psutil.Process().memory_info().peak_wset / 2**20)})
