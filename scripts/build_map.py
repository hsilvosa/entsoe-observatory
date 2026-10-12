"""Build the zone map for the dashboard from Natural Earth (public domain).

Countries come from admin-0. Zones inside NO, SE, DK and IT are unions of admin-1 regions, so
their borders follow administrative lines and only approximate the real bidding zone borders.
Output: dashboard/map-zones.json with one SVG path per zone in a Lambert azimuthal
equal-area projection (viewBox 0 0 1000 1000) and a label point.

Usage: uv run --with shapely python scripts/build_map.py <countries.geojson> <admin1.geojson>
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon, box, shape
from shapely.ops import unary_union

EUROPE = box(-12.5, 34.0, 45.0, 72.0)  # drops overseas territories, Azores, Canaries, Svalbard
LON0, LAT0 = 12.0, 52.0
SCALE, CX, CY = 1.25, 500, 520
COUNTRIES = {
    "AL": ["ALB"], "AT": ["AUT"], "BA": ["BIH"], "BE": ["BEL"], "BG": ["BGR"],
    "CH": ["CHE"], "CY": ["CYP"], "CZ": ["CZE"], "DE_LU": ["DEU", "LUX"], "EE": ["EST"],
    "ES": ["ESP"], "FI": ["FIN"], "FR": ["FRA"], "GR": ["GRC"], "HR": ["HRV"], "HU": ["HUN"],
    "LT": ["LTU"], "LU": [], "LV": ["LVA"], "ME": ["MNE"], "MK": ["MKD"], "MT": ["MLT"],
    "NL": ["NLD"], "PL": ["POL"], "PT": ["PRT"], "RO": ["ROU"], "RS": ["SRB"], "SI": ["SVN"],
    "SK": ["SVK"], "TR": ["TUR"], "XK": ["KOS"],
}
REGIONS = {  # zone -> (country, admin-1 names)
    "DK_1": ("DNK", ["Nordjylland", "Midtjylland", "Syddanmark"]),
    "DK_2": ("DNK", ["Hovedstaden", "Sjaælland"]),
    "NO_1": ("NOR", ["Oslo", "Akershus", "Østfold", "Hedmark", "Oppland", "Buskerud",
                     "Vestfold", "Telemark"]),
    "NO_2": ("NOR", ["Aust-Agder", "Vest-Agder", "Rogaland"]),
    "NO_3": ("NOR", ["Møre og Romsdal", "Sør-Trøndelag", "Nord-Trøndelag"]),
    "NO_4": ("NOR", ["Nordland", "Troms", "Finnmark"]),
    "NO_5": ("NOR", ["Hordaland", "Sogn og Fjordane"]),
    "SE_1": ("SWE", ["Norrbotten", "Västerbotten"]),
    "SE_2": ("SWE", ["Västernorrland", "Jämtland"]),
    "SE_3": ("SWE", ["Gävleborg", "Dalarna", "Värmland", "Uppsala", "Västmanland", "Orebro",
                     "Stockholm", "Södermanland", "Östergötland", "Västra Götaland",
                     "Jönköping", "Kalmar", "Gotland"]),
    "SE_4": ("SWE", ["Skåne", "Blekinge", "Halland", "Kronoberg"]),
}
ITALY = {  # zone -> admin-1 'region' values
    "IT_NORD": ["Piemonte", "Valle d'Aosta", "Lombardia", "Trentino-Alto Adige", "Veneto",
                "Friuli-Venezia Giulia", "Liguria", "Emilia-Romagna"],
    "IT_CNOR": ["Toscana", "Umbria", "Marche"],
    "IT_CSUD": ["Lazio", "Abruzzo", "Campania"],
    "IT_SUD": ["Molise", "Apulia", "Basilicata", "Calabria"],
    "IT_SARD": ["Sardegna"],
    "IT_SICI": ["Sicily"],
}


def project(lon: float, lat: float) -> tuple[float, float]:
    l0, p0 = math.radians(LON0), math.radians(LAT0)
    lam, phi = math.radians(lon), math.radians(lat)
    k = math.sqrt(2 / (1 + math.sin(p0) * math.sin(phi)
                       + math.cos(p0) * math.cos(phi) * math.cos(lam - l0)))
    x = k * math.cos(phi) * math.sin(lam - l0)
    y = k * (math.cos(p0) * math.sin(phi) - math.sin(p0) * math.cos(phi) * math.cos(lam - l0))
    return CX + x * 1000 * SCALE, CY - y * 1000 * SCALE


def to_path(geom: Polygon | MultiPolygon, min_area: float) -> str:
    polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms)
    parts: list[str] = []
    for poly in polys:
        if poly.area < min_area:
            continue
        for ring in [poly.exterior, *poly.interiors]:
            pts = [project(x, y) for x, y in ring.coords]
            parts.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
    return "".join(parts)


def main(countries_file: str, admin1_file: str) -> None:
    countries = json.loads(Path(countries_file).read_text(encoding="utf-8"))["features"]
    admin1 = json.loads(Path(admin1_file).read_text(encoding="utf-8"))["features"]
    by_iso = {f["properties"]["ADM0_A3"]: shape(f["geometry"]) for f in countries}
    zones: dict[str, object] = {}
    for key, isos in COUNTRIES.items():
        if isos:
            zones[key] = unary_union([by_iso[i] for i in isos])
    ireland = by_iso["IRL"]
    north = [shape(f["geometry"]) for f in admin1
             if f["properties"]["adm0_a3"] == "GBR"
             and f["properties"]["name"] == "Northern Ireland"]
    zones["IE_SEM"] = unary_union([ireland, *north])
    for key, (iso, names) in REGIONS.items():
        parts = [shape(f["geometry"]) for f in admin1
                 if f["properties"]["adm0_a3"] == iso and f["properties"]["name"] in names]
        assert len(parts) == len(names), (key, len(parts), len(names))
        zones[key] = unary_union(parts)
    for key, regions in ITALY.items():
        parts = [shape(f["geometry"]) for f in admin1
                 if f["properties"]["adm0_a3"] == "ITA"
                 and f["properties"].get("region") in regions]
        assert parts, key
        zones[key] = unary_union(parts)
    out: dict[str, dict[str, object]] = {}
    for key, geom in zones.items():
        clipped = geom.intersection(EUROPE).simplify(0.04, preserve_topology=True)
        if clipped.is_empty:
            continue
        biggest = max(clipped.geoms, key=lambda g: g.area) if hasattr(clipped, "geoms") else clipped
        point = biggest.representative_point()
        x, y = project(point.x, point.y)
        out[key] = {"d": to_path(clipped, 0.02), "label": [round(x, 1), round(y, 1)]}
    coords = [
        tuple(map(float, pair.split(",")))
        for zone in out.values()
        for pair in re.findall(r"-?\d+\.?\d*,-?\d+\.?\d*", str(zone["d"]))
    ]
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    pad = 12
    box_ = [round(min(xs) - pad), round(min(ys) - pad),
            round(max(xs) - min(xs) + 2 * pad), round(max(ys) - min(ys) + 2 * pad)]
    payload: dict[str, object] = {"_viewBox": box_, **out}
    target = Path(__file__).resolve().parents[1] / "dashboard" / "map-zones.json"
    target.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(len(out), "zones,", target.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
