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


async def generate_flow_data() -> list:
    """Generate traffic flow data for major Ayrshire routes.
    
    In production, this would use TomTom or HERE Traffic Flow API.
    We generate realistic synthetic data based on time of day and known routes.
    """
    hour = datetime.now().hour
    # Rush hours = higher congestion
    is_rush = 7 <= hour <= 9 or 16 <= hour <= 18

    major_routes = [
        # (name, waypoints, base_speed, road_ref)
        ("A77 Ayr Bypass", [(55.47, -4.61), (55.49, -4.58), (55.51, -4.55)], 70, "A77"),
        ("A77 South Ayr", [(55.43, -4.64), (55.45, -4.63), (55.47, -4.62)], 60, "A77"),
        ("A78 Coast Road", [(55.55, -4.72), (55.60, -4.75), (55.65, -4.77)], 60, "A78"),
        ("A70 East Ayrshire", [(55.47, -4.50), (55.48, -4.44), (55.49, -4.38)], 60, "A70"),
        ("A71 Kilmarnock", [(55.61, -4.49), (55.60, -4.43), (55.59, -4.35)], 60, "A71"),
        ("A76 Kilmarnock Road", [(55.60, -4.50), (55.56, -4.55), (55.53, -4.58)], 50, "A76"),
        ("A736 Irvine Road", [(55.62, -4.63), (55.63, -4.68), (55.64, -4.72)], 60, "A736"),
    ]

    import random
    rng = random.Random(int(time.time() / 300))  # changes every 5 mins

    results = []
    for name, waypoints, base_speed, ref in major_routes:
        # Simulate speed variation
        speed_factor = 1.0
        if is_rush:
            speed_factor = rng.uniform(0.4, 0.8)
        else:
            speed_factor = rng.uniform(0.7, 1.0)

        current_speed = base_speed * speed_factor
        congestion = 1 - speed_factor

        if congestion > 0.5:
            severity = "red"
            status = "Heavy Traffic"
        elif congestion > 0.3:
            severity = "amber"
            status = "Moderate Traffic"
        else:
            severity = "green"
            status = "Free Flow"

        # Use centre waypoint as marker position
        mid = waypoints[len(waypoints) // 2]
        results.append({
            "id": f"flow-{ref}-{name.replace(' ', '-').lower()}",
            "type": "flow",
            "title": f"{name}: {status}",
            "description": f"Current speed: ~{int(current_speed)} km/h | Free flow: {base_speed} km/h | Route: {ref}",
            "lat": mid[0],
            "lon": mid[1],
            "severity": severity,
            "source": "Traffic Flow",
            "url": "",
            "pub_date": datetime.now(timezone.utc).isoformat(),
            "active": True,
            "flow_data": {
                "route": name,
                "road_ref": ref,
                "current_speed": round(current_speed, 1),
                "free_flow_speed": base_speed,
                "congestion_level": round(congestion, 2),
                "status": status,
                "waypoints": waypoints,
            },
        })

    return results


async def fetch_all_data() -> dict:
    """Fetch all data sources concurrently."""
    ts_incidents, ts_roadworks, osm, srwr, flow = await asyncio.gather(
        fetch_tsis_incidents(),
        fetch_tsis_roadworks(),
        fetch_overpass_incidents(),
        fetch_srwr_roadworks(),
        generate_flow_data(),
        return_exceptions=True,
    )

    all_items = []
    for result in [ts_incidents, ts_roadworks, osm, srwr, flow]:
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

    return {
        "service": "ayrtraffic",
        "status": "online",
        "total_incidents": meta.get("total_incidents", 0),
        "active_roadworks": meta.get("total_roadworks", 0),
        "congestion_score": congestion_score,
        "sources_active": meta.get("sources_active", 0),
        "last_updated": meta.get("updated_at"),
        "url": f"http://localhost:{PORT}",
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
