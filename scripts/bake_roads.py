#!/usr/bin/env python3
"""
Pre-bake Ayrshire trunk road geometries from OpenStreetMap (Overpass API).

Run once whenever the OSM data needs refreshing — output saved to
data/roads/{REF}.json as a list of polylines, where each polyline is
[[lat, lon], …]. Each OSM way that shares the road's `ref` becomes one
polyline (so a single trunk road typically yields tens of polylines that
together cover the whole route).

The bbox extends slightly north of Ayrshire so the M77 reaches Glasgow
(M8 J22) and the A77 reaches its commuter approach to Glasgow.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = "https://overpass-api.de/api/interpreter"

# south, west, north, east — covers Ayrshire including M77 north into Glasgow
BBOX = (55.05, -5.10, 55.95, -4.05)

TRUNK_ROADS = ["M77", "A77", "A78", "A70", "A71", "A76", "A736", "A737", "A719"]

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "roads"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_road(ref: str) -> list:
    s, w, n, e = BBOX
    # Match exact ref AND refs with separators (e.g. "A77;A78") for shared sections
    query = f"""[out:json][timeout:60];
(
  way["ref"="{ref}"]["highway"]({s},{w},{n},{e});
  way["ref"~"(^|;){ref}(;|$)"]["highway"]({s},{w},{n},{e});
);
out geom;
"""
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        OVERPASS, data=data, method="POST",
        headers={
            "User-Agent": "ayrtraffic-bake/1.0 (https://traffic.ayrshire.wispayr.online)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        d = json.load(resp)

    polylines = []
    seen_ways = set()
    for el in d.get("elements", []):
        if el.get("type") != "way":
            continue
        wid = el.get("id")
        if wid in seen_ways:
            continue
        seen_ways.add(wid)
        geom = el.get("geometry") or []
        line = [[round(g["lat"], 6), round(g["lon"], 6)] for g in geom if "lat" in g and "lon" in g]
        if len(line) >= 2:
            polylines.append(line)
    return polylines


def main():
    overall_pts = 0
    overall_ways = 0
    for ref in TRUNK_ROADS:
        print(f"  {ref:<6} ", end="", flush=True)
        try:
            polylines = fetch_road(ref)
            (OUT_DIR / f"{ref}.json").write_text(json.dumps(polylines))
            pts = sum(len(p) for p in polylines)
            overall_pts += pts
            overall_ways += len(polylines)
            print(f"{len(polylines):>3} ways, {pts:>5} points")
        except Exception as e:
            print(f"FAIL: {e}")
        time.sleep(2)  # be polite to overpass

    print(f"\nTotal: {overall_ways} ways, {overall_pts} points across {len(TRUNK_ROADS)} roads")
    print(f"Output: {OUT_DIR}")


if __name__ == "__main__":
    main()
