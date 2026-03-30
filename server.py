"""
AyrTraffic — Live Traffic Map for Ayrshire
FastAPI backend: fetches, caches & serves traffic data
Port: 3870
"""

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ─── Config ──────────────────────────────────────────────────────────────────

PORT = 3870
CACHE_TTL = 180  # 3 minutes
AYRSHIRE_BBOX = {
    "south": 55.25,
    "north": 55.75,
    "west": -5.00,
    "east": -4.10,
}
NURO_HUB = "http://192.168.195.33:3960"
SERVICE_ID = "ayrtraffic"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [AyrTraffic] %(message)s")
log = logging.getLogger("ayrtraffic")

# ─── Database ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "data" / "cache.db"
DB_PATH.parent.mkdir(exist_ok=True)


def db_connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
    log.info("Database initialised")


def cache_get(key: str) -> Optional[dict]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT data, updated_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    age = time.time() - row["updated_at"]
    if age > CACHE_TTL:
        return None
    return json.loads(row["data"])


def cache_set(key: str, data: dict):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, data, updated_at) VALUES (?,?,?)",
            (key, json.dumps(data), time.time()),
        )
        conn.commit()


def cache_get_stale(key: str) -> Optional[dict]:
    """Return cached data even if stale (for fallback)."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT data FROM cache WHERE key = ?", (key,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def in_ayrshire(lat: float, lon: float) -> bool:
    b = AYRSHIRE_BBOX
    return b["south"] <= lat <= b["north"] and b["west"] <= lon <= b["east"]


def parse_coords_from_text(text: str) -> Optional[tuple]:
    """Extract lat/lon from description text like 'Lat: 55.46, Lon: -4.63'."""
    pat = r"[Ll]at(?:itude)?[:\s]+(-?\d+\.?\d*)[,;\s]+[Ll]on(?:gitude)?[:\s]+(-?\d+\.?\d*)"
    m = re.search(pat, text or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def coords_from_road(road: str) -> Optional[tuple]:
    """Approximate coords for Ayrshire roads when no coords given."""
    road_coords = {
        "A77": (55.47, -4.61),
        "A78": (55.63, -4.75),
        "A70": (55.47, -4.50),
        "A71": (55.52, -4.35),
        "A76": (55.50, -4.55),
        "A736": (55.60, -4.60),
        "A737": (55.62, -4.65),
        "M77": (55.61, -4.43),
        "A713": (55.30, -4.40),
        "A719": (55.37, -4.65),
        "A726": (55.60, -4.50),
    }
    for k, v in road_coords.items():
        if k in (road or "").upper():
            return v
    # Default to Ayr centre with some jitter
    import random
    return (55.45 + random.uniform(-0.1, 0.1), -4.62 + random.uniform(-0.15, 0.15))


def severity_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ["severe", "major", "accident", "collision", "obstruction", "emergency"]):
        return "red"
    if any(w in t for w in ["roadworks", "works", "delay", "closure", "diversion"]):
        return "amber"
    return "green"


# ─── Data Fetchers ────────────────────────────────────────────────────────────

async def fetch_tsis_incidents() -> list:
    """Fetch live incidents from Traffic Scotland TSIS JSON API."""
    url = "https://www.traffic.gov.scot/tsis/incidents"
    results = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"User-Agent": "AyrTraffic/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("results", data if isinstance(data, list) else [])
                for item in items:
                    try:
                        lat = float(item.get("lat") or 0)
                        lon = float(item.get("lng") or 0)
                    except (ValueError, TypeError):
                        continue
                    if not lat or not lon:
                        continue
                    if not in_ayrshire(lat, lon):
                        continue

                    desc_raw = item.get("description") or ""
                    desc = re.sub(r"<[^>]+>", " ", desc_raw).strip()
                    title = (
                        item.get("location_name")
                        or item.get("road_name")
                        or item.get("title")
                        or "Incident"
                    )
                    itype = item.get("incident_type_name") or "Incident"
                    subtype = item.get("incident_sub_type_name") or ""
                    if subtype:
                        title = f"{title} — {subtype}"

                    sev = severity_from_text(itype + " " + (desc or "") + " " + subtype)
                    ts = item.get("start_time") or item.get("date")
                    pub_date = ""
                    if ts:
                        try:
                            pub_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                        except Exception:
                            pass

                    results.append({
                        "id": f"tsis-inc-{item.get('incident_id', item.get('sid', ''))}",
                        "type": "incidents",
                        "title": title,
                        "description": desc or item.get("direction_name") or "",
                        "lat": lat,
                        "lon": lon,
                        "severity": sev,
                        "source": "Traffic Scotland",
                        "url": f"https://www.traffic.gov.scot/travel-news/incidents",
                        "pub_date": pub_date,
                        "active": True,
                        "incident_type": itype,
                    })
        except Exception as e:
            log.warning(f"TSIS incidents fetch failed: {e}")
    log.info(f"TSIS incidents: {len(results)} Ayrshire items")
    return results


async def fetch_tsis_roadworks() -> list:
    """Fetch roadworks from Traffic Scotland TSIS JSON API."""
    url = "https://www.traffic.gov.scot/tsis/roadworks"
    results = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"User-Agent": "AyrTraffic/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("results", data if isinstance(data, list) else [])
                for item in items:
                    try:
                        lat = float(item.get("lat") or 0)
                        lon = float(item.get("lng") or 0)
                    except (ValueError, TypeError):
                        continue
                    if not lat or not lon:
                        continue
                    if not in_ayrshire(lat, lon):
                        continue

                    desc_raw = item.get("description") or item.get("delay_information") or ""
                    desc = re.sub(r"<[^>]+>", " ", desc_raw).strip()[:300]
                    title = (
                        item.get("location_name")
                        or item.get("extra_location_details")
                        or "Roadworks"
                    )
                    direction = item.get("direction_text") or ""
                    if direction:
                        desc = f"{direction}: {desc}"

                    ts = item.get("start_datetime")
                    pub_date = ""
                    if ts:
                        try:
                            pub_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                        except Exception:
                            pass

                    results.append({
                        "id": f"tsis-rw-{item.get('roadwork_id', item.get('sid', ''))}",
                        "type": "roadworks",
                        "title": title,
                        "description": desc,
                        "lat": lat,
                        "lon": lon,
                        "severity": "amber",
                        "source": "Traffic Scotland",
                        "url": "https://www.traffic.gov.scot/travel-news/roadworks",
                        "pub_date": pub_date,
                        "active": True,
                    })
        except Exception as e:
            log.warning(f"TSIS roadworks fetch failed: {e}")
    log.info(f"TSIS roadworks: {len(results)} Ayrshire items")
    return results


async def fetch_overpass_incidents() -> list:
    """Fetch road closures, construction from OSM Overpass API."""
    query = f"""
    [out:json][timeout:25][bbox:{AYRSHIRE_BBOX['south']},{AYRSHIRE_BBOX['west']},{AYRSHIRE_BBOX['north']},{AYRSHIRE_BBOX['east']}];
    (
      way["highway"]["construction"];
      way["highway"]["access"="no"];
      node["amenity"="police"]["emergency"="yes"];
    );
    out center;
    """

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
                headers={"User-Agent": "AyrTraffic/1.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = []
                for el in data.get("elements", [])[:20]:  # cap at 20
                    lat = el.get("lat") or (el.get("center") or {}).get("lat")
                    lon = el.get("lon") or (el.get("center") or {}).get("lon")
                    if not lat or not lon:
                        continue
                    tags = el.get("tags", {})
                    name = tags.get("name") or tags.get("ref") or "Road works"
                    highway = tags.get("highway", "road")
                    desc = f"{highway.title()} - {name}"
                    if tags.get("construction"):
                        desc = f"Construction: {tags['construction']} - {name}"
                    results.append({
                        "id": f"osm-{el['id']}",
                        "type": "roadworks",
                        "title": f"OSM: {name}",
                        "description": desc,
                        "lat": lat,
                        "lon": lon,
                        "severity": "amber",
                        "source": "OpenStreetMap",
                        "url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                        "pub_date": "",
                        "active": True,
                    })
                log.info(f"Overpass: got {len(results)} items")
                return results
        except Exception as e:
            log.warning(f"Overpass fetch failed: {e}")
    return []


async def fetch_srwr_roadworks() -> list:
    """Scottish Road Works Register — public API."""
    # SRWR has a public WFS endpoint
    url = (
        "https://www.roadworksscotland.org/api/OneScotland"
        "/GetPermits?organisation=&street=&town=ayrshire&pageSize=20&currentPage=1"
    )
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    results = []
                    permits = data.get("permits", data.get("result", data if isinstance(data, list) else []))
                    for permit in permits[:30]:
                        lat = permit.get("lat") or permit.get("latitude")
                        lon = permit.get("lon") or permit.get("longitude") or permit.get("lng")
                        if not lat or not lon:
                            continue
                        try:
                            lat, lon = float(lat), float(lon)
                        except (ValueError, TypeError):
                            continue
                        if not in_ayrshire(lat, lon):
                            continue
                        results.append({
                            "id": f"srwr-{permit.get('permitReference', permit.get('id', hash(str(permit))))}",
                            "type": "roadworks",
                            "title": permit.get("workDescription") or permit.get("description") or "Roadworks",
                            "description": f"Works by: {permit.get('promoter', 'Unknown')} | Status: {permit.get('workStatus', 'Active')}",
                            "lat": lat,
                            "lon": lon,
                            "severity": "amber",
                            "source": "SRWR",
                            "url": "https://www.roadworksscotland.org",
                            "pub_date": permit.get("proposedStartDate", ""),
                            "active": True,
                        })
                    log.info(f"SRWR: got {len(results)} items")
                    return results
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"SRWR fetch failed: {e}")
    return []


async def fetch_ara_tros() -> list:
    """Fetch live TTROs from Ayrshire Roads Alliance GeoJSON API."""
    results = []
    url = "https://ara.roadsonline.co.uk/ARA/TTRO/MapData/Restrictions"

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                geojson = resp.json()
                features = geojson.get("features", [])

                # Deduplicate by restriction ID (many line segments per restriction)
                seen_ids = set()
                for feat in features:
                    props = feat.get("properties", {})
                    rid = props.get("id") or props.get("reference", "")
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)

                    # Get centroid from geometry
                    geom = feat.get("geometry", {})
                    coords = geom.get("coordinates", [])
                    if geom["type"] == "Point":
                        lon, lat = coords[0], coords[1]
                    elif geom["type"] == "LineString" and coords:
                        mid = coords[len(coords) // 2]
                        lon, lat = mid[0], mid[1]
                    else:
                        continue

                    title = props.get("title", "Unknown TRO")
                    rtype = props.get("type", "unknown")
                    road = props.get("road", "")
                    town = props.get("town", "")
                    dates = props.get("restrictionsInPlace", "")
                    ref = props.get("reference", "")
                    is_urgent = props.get("isUrgent", False)

                    # Map ARA types to severity
                    severity_map = {
                        "roadClosure": "red",
                        "trafficSignalPermit": "amber",
                        "parking": "purple",
                        "speedLimit": "blue",
                    }
                    severity = severity_map.get(rtype, "purple")
                    if is_urgent:
                        severity = "red"

                    desc_parts = []
                    if road:
                        desc_parts.append(f"Road: {road}")
                    if town:
                        desc_parts.append(f"Town: {town}")
                    if dates:
                        desc_parts.append(f"Period: {dates}")
                    if ref:
                        desc_parts.append(f"Ref: {ref}")
                    desc_parts.append(f"Type: {rtype}")

                    results.append({
                        "id": f"ara-{ref or rid}",
                        "type": "tro",
                        "title": title,
                        "description": " | ".join(desc_parts),
                        "lat": lat,
                        "lon": lon,
                        "severity": severity,
                        "source": "Ayrshire Roads Alliance",
                        "url": f"https://ara.roadsonline.co.uk/ARA/TTRO/Map",
                        "pub_date": dates.split(" - ")[0] if " - " in dates else "",
                        "active": True,
                    })

                log.info(f"ARA TROs: got {len(results)} live restrictions (from {len(features)} features)")
            else:
                log.warning(f"ARA TRO API returned {resp.status_code}")
        except Exception as e:
            log.warning(f"ARA TRO fetch failed: {e}")

    return results


TOMTOM_API_KEY = "KkNkcnBcWERbYBeSXUPV9DN1VWZOWbal"


TOMTOM_CACHE_TTL = 600  # 10 minutes — keeps us under 2500 daily free tier (17 probes × 144 = 2448)


async def generate_flow_data() -> list:
    """Fetch real-time traffic flow from TomTom for major Ayrshire roads.
    
    Rate limited: 10-min cache per probe to stay under 2500 daily requests.
    """
    # Check TomTom cache first
    cached = cache_get_stale("tomtom_flow")
    if cached:
        # Check if cache is still within TTL
        row = DB.execute("SELECT timestamp FROM cache WHERE key = 'tomtom_flow'").fetchone()
        if row:
            age = time.time() - row[0]
            if age < TOMTOM_CACHE_TTL:
                log.debug(f"TomTom: serving cached data ({int(age)}s old, TTL={TOMTOM_CACHE_TTL}s)")
                return cached.get("items", [])

    # Probe points on key Ayrshire roads (lat, lon, road_name, road_ref)
    PROBE_POINTS = [
        (55.4750, -4.5900, "A77 Ayr Bypass", "A77"),
        (55.4400, -4.6350, "A77 South Ayr", "A77"),
        (55.5800, -4.7400, "A78 Irvine to Saltcoats", "A78"),
        (55.6300, -4.7700, "A78 Ardrossan", "A78"),
        (55.4800, -4.4400, "A70 Ayr to Cumnock", "A70"),
        (55.6100, -4.4900, "A71 Kilmarnock", "A71"),
        (55.5600, -4.5500, "A76 Kilmarnock Road", "A76"),
        (55.6300, -4.6800, "A736 Irvine Road", "A736"),
        (55.5430, -4.6600, "A79 Troon", "A79"),
        (55.3540, -4.6810, "A77 Maybole", "A77"),
        (55.6115, -4.4955, "Kilmarnock Centre", "A"),
        (55.4583, -4.6292, "Ayr Centre", "A"),
        (55.5560, -4.7750, "Irvine Centre", "A"),
        (55.6410, -4.8120, "Ardrossan Harbour", "A"),
        (55.2420, -4.8580, "A77 Girvan", "A77"),
        (55.7530, -4.8530, "A78 Fairlie to Largs", "A78"),
        (55.4540, -4.2660, "A76 Cumnock", "A76"),
    ]

    results = []
    async with httpx.AsyncClient(timeout=10) as client:
        for lat, lon, name, ref in PROBE_POINTS:
            try:
                url = (
                    f"https://api.tomtom.com/traffic/services/4/flowSegmentData/"
                    f"absolute/10/json?key={TOMTOM_API_KEY}&point={lat},{lon}"
                )
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json().get("flowSegmentData", {})
                    current_speed = data.get("currentSpeed", 0)
                    free_flow = data.get("freeFlowSpeed", 60)
                    road_closure = data.get("roadClosure", False)
                    confidence = data.get("confidence", 0)

                    if free_flow > 0:
                        congestion = max(0, 1 - (current_speed / free_flow))
                    else:
                        congestion = 0

                    if road_closure:
                        severity = "red"
                        status = "Road Closed"
                    elif congestion > 0.5:
                        severity = "red"
                        status = "Heavy Traffic"
                    elif congestion > 0.3:
                        severity = "amber"
                        status = "Moderate Traffic"
                    else:
                        severity = "green"
                        status = "Free Flow"

                    # Extract road geometry from TomTom response
                    coords = data.get("coordinates", {}).get("coordinate", [])

                    results.append({
                        "id": f"flow-{ref}-{name.replace(' ', '-').lower()}",
                        "type": "flow",
                        "title": f"{name}: {status}",
                        "description": f"Speed: {int(current_speed)} km/h | Free flow: {free_flow} km/h | Confidence: {confidence:.0%}",
                        "lat": lat,
                        "lon": lon,
                        "severity": severity,
                        "source": "TomTom",
                        "url": "",
                        "pub_date": datetime.now(timezone.utc).isoformat(),
                        "active": True,
                        "flow_data": {
                            "route": name,
                            "road_ref": ref,
                            "current_speed": round(current_speed, 1),
                            "free_flow_speed": free_flow,
                            "congestion_level": round(congestion, 2),
                            "status": status,
                            "road_closure": road_closure,
                            "confidence": confidence,
                            "geometry": [{"lat": c["latitude"], "lon": c["longitude"]} for c in coords[:20]],
                        },
                    })
            except Exception as e:
                log.warning(f"TomTom flow probe failed for {name}: {e}")
                continue

    log.info(f"TomTom: got {len(results)} flow segments from {len(PROBE_POINTS)} probes")
    # Cache results with separate TTL
    cache_set("tomtom_flow", {"items": results})
    return results


async def fetch_all_data() -> dict:
    """Fetch all data sources concurrently."""
    ts_incidents, ts_roadworks, osm, srwr, flow, tros = await asyncio.gather(
        fetch_tsis_incidents(),
        fetch_tsis_roadworks(),
        fetch_overpass_incidents(),
        fetch_srwr_roadworks(),
        generate_flow_data(),
        fetch_ara_tros(),
        return_exceptions=True,
    )

    all_items = []
    for result in [ts_incidents, ts_roadworks, osm, srwr, flow, tros]:
        if isinstance(result, list):
            all_items.extend(result)

    incidents = [i for i in all_items if i["type"] == "incidents"]
    roadworks = [i for i in all_items if i["type"] in ("roadworks", "conditions")]
    tros = [i for i in all_items if i["type"] == "tro"]
    flow_items = [i for i in all_items if i["type"] == "flow"]

    # Deduplicate by approximate proximity (50m)
    def deduplicate(items):
        seen = []
        out = []
        for item in items:
            lat, lon = item["lat"], item["lon"]
            dup = False
            for s in seen:
                dlat = (lat - s[0]) ** 2
                dlon = (lon - s[1]) ** 2
                if math.sqrt(dlat + dlon) < 0.0005:
                    dup = True
                    break
            if not dup:
                seen.append((lat, lon))
                out.append(item)
        return out

    return {
        "incidents": deduplicate(incidents),
        "roadworks": deduplicate(roadworks),
        "tros": deduplicate(tros),
        "flow": flow_items,
        "all": deduplicate(all_items),
        "meta": {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_incidents": len(incidents),
            "total_roadworks": len(roadworks),
            "total_tros": len(tros),
            "sources_active": sum([
                1 if ts_incidents and not isinstance(ts_incidents, Exception) else 0,
                1 if ts_roadworks and not isinstance(ts_roadworks, Exception) else 0,
                1,  # OSM / Overpass
                1,  # Flow
                1 if tros and not isinstance(tros, Exception) else 0,  # ARA + Transport Scotland TROs
            ]),
        },
    }


# ─── Background Refresh ───────────────────────────────────────────────────────

_refresh_task = None


async def refresh_loop():
    while True:
        try:
            log.info("Refreshing traffic data...")
            data = await fetch_all_data()
            cache_set("all_traffic", data)
            log.info(
                f"Refresh complete: {data['meta']['total_incidents']} incidents, "
                f"{data['meta']['total_roadworks']} roadworks"
            )
        except Exception as e:
            log.error(f"Refresh failed: {e}")
        await asyncio.sleep(CACHE_TTL)


async def nuro_heartbeat_loop():
    """Send heartbeat to nuro hub."""
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                data = cache_get_stale("all_traffic") or {}
                meta = data.get("meta", {})
                await client.post(
                    f"{NURO_HUB}/api/services/heartbeat",
                    json={
                        "service": SERVICE_ID,
                        "status": "online",
                        "port": PORT,
                        "description": "AyrTraffic — Live Traffic Map for Ayrshire",
                        "url": f"http://localhost:{PORT}",
                        "stats": {
                            "incidents": meta.get("total_incidents", 0),
                            "roadworks": meta.get("total_roadworks", 0),
                            "sources": meta.get("sources_active", 0),
                            "last_refresh": meta.get("updated_at"),
                        },
                    },
                )
            except Exception:
                pass  # nuro hub may not be running, that's fine
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    # Initial data fetch
    asyncio.create_task(asyncio.sleep(0))  # yield to event loop
    global _refresh_task
    _refresh_task = asyncio.create_task(refresh_loop())
    asyncio.create_task(nuro_heartbeat_loop())
    yield
    if _refresh_task:
        _refresh_task.cancel()


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="AyrTraffic", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


# ─── API Routes ───────────────────────────────────────────────────────────────

@app.get("/api/traffic")
async def get_traffic():
    """All traffic data: incidents, roadworks, TROs, flow."""
    data = cache_get("all_traffic")
    if data is None:
        # Not yet cached — fetch now
        data = await fetch_all_data()
        cache_set("all_traffic", data)
    return JSONResponse(data)


@app.get("/api/traffic/incidents")
async def get_incidents():
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse({"items": data.get("incidents", []), "meta": data.get("meta", {})})


@app.get("/api/traffic/roadworks")
async def get_roadworks():
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse({"items": data.get("roadworks", []), "meta": data.get("meta", {})})


@app.get("/api/traffic/flow")
async def get_flow():
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse({"items": data.get("flow", []), "meta": data.get("meta", {})})


@app.get("/api/nuro")
async def get_nuro_stats():
    """nuro integration endpoint — exposes key stats."""
    data = cache_get_stale("all_traffic") or {}
    meta = data.get("meta", {})
    flow = data.get("flow", [])

    # Congestion score: 0-100
    if flow:
        avg_congestion = sum(
            f.get("flow_data", {}).get("congestion_level", 0) for f in flow
        ) / len(flow)
        congestion_score = round(avg_congestion * 100)
    else:
        congestion_score = 0

    total_incidents = meta.get("total_incidents", 0)
    total_roadworks = meta.get("total_roadworks", 0)
    sources_active = meta.get("sources_active", 0)
    updated_at = meta.get("updated_at")

    # Severity based on incidents
    inc_severity = "critical" if total_incidents > 5 else "warning" if total_incidents > 0 else "ok"
    cong_severity = "critical" if congestion_score > 60 else "warning" if congestion_score > 30 else "ok"

    # Build v2 streams for nuro adaptive viz
    streams = [
        {
            "id": "incidents",
            "type": "scalar",
            "value": total_incidents,
            "label": "Active Incidents",
            "unit": "",
            "render": "stat",
            "range": [0, 20],
            "thresholds": {"good": 0, "warn": 3, "crit": 8},
            "severity": inc_severity,
            "updated": updated_at,
        },
        {
            "id": "roadworks",
            "type": "scalar",
            "value": total_roadworks,
            "label": "Roadworks",
            "unit": "active",
            "render": "stat",
            "range": [0, 100],
            "thresholds": {"good": 20, "warn": 40, "crit": 60},
            "severity": "warning" if total_roadworks > 30 else "ok",
            "updated": updated_at,
        },
        {
            "id": "congestion",
            "type": "scalar",
            "value": congestion_score,
            "label": "Congestion",
            "unit": "%",
            "render": "gauge",
            "range": [0, 100],
            "thresholds": {"good": 25, "warn": 50, "crit": 75},
            "severity": cong_severity,
            "updated": updated_at,
        },
        {
            "id": "sources",
            "type": "scalar",
            "value": sources_active,
            "label": "Data Sources",
            "unit": "active",
            "render": "badge",
            "severity": "ok" if sources_active >= 3 else "warning",
            "updated": updated_at,
        },
    ]

    # Per-road flow streams
    for f in flow[:8]:
        fd = f.get("flow_data", {})
        speed = fd.get("current_speed", 0)
        free_flow = fd.get("free_flow_speed", 60)
        route = fd.get("route", f.get("title", "Unknown"))
        cong = fd.get("congestion_level", 0)
        streams.append({
            "id": f"flow_{route.lower().replace(' ', '_')}",
            "type": "scalar",
            "value": speed,
            "label": route,
            "unit": "km/h",
            "render": "bar",
            "range": [0, free_flow],
            "thresholds": {"good": free_flow * 0.7, "warn": free_flow * 0.4, "crit": free_flow * 0.2},
            "severity": "critical" if cong > 0.6 else "warning" if cong > 0.3 else "ok",
            "updated": updated_at,
        })

    return {
        "service": "ayrtraffic",
        "version": "2.0",
        "label": "AyrTraffic",
        "icon": "🚦",
        "url": "https://traffic.ayrshire.wispayr.online",
        "status": "online",
        "total_incidents": total_incidents,
        "active_roadworks": total_roadworks,
        "congestion_score": congestion_score,
        "sources_active": sources_active,
        "last_updated": updated_at,
        "streams": streams,
    }


@app.post("/api/refresh")
async def force_refresh():
    """Force a data refresh."""
    data = await fetch_all_data()
    cache_set("all_traffic", data)
    return {"ok": True, "meta": data["meta"]}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ayrtraffic", "port": PORT}


# ─── Static / Frontend ────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "Frontend not built"}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)
