"""
AyrTraffic — Live Traffic Map for Ayrshire
FastAPI backend: fetches, caches & serves traffic data
Port: 3870
"""

import asyncio
import csv
import ftplib
import io
import json
import logging
import math
import os
import re
import shutil
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

import studio as nar_studio

# ─── Config ──────────────────────────────────────────────────────────────────

PORT = 3876
CACHE_TTL = 180  # 3 minutes
FERRY_CACHE_TTL = 300  # 5 minutes
AYRSHIRE_BBOX = {
    "south": 55.25,
    "north": 55.75,
    "west": -5.00,
    "east": -4.10,
}
NURO_HUB = "http://10.200.0.8:3960"
SERVICE_ID = "ayrtraffic"
SIPHON_URL = "http://localhost:3883"
# ─── DATEX II Config (Traffic Scotland) ──────────────────────────────────────

DATEX2_CLIENT_ID = "14d2c05d-696d-45be-8ddc-2b20105de3c7"
DATEX2_CLIENT_KEY = "dQgBVutS1sbmarOqsGx97Whv9wVYZv8U60X5dM7ag9KGIXAyV09S9A0jsIXYW3XC"
DATEX2_BASE_URL = "https://datex2.trafficscotland.org/rest/2.3/publications"
DATEX2_AUTH = (DATEX2_CLIENT_ID, DATEX2_CLIENT_KEY)
DATEX2_NS = {"d2": "http://datex2.eu/schema/2/2_0"}
DATEX2_CACHE_TTL = 300  # 5 minutes

# ─── FTP Camera Config (Traffic Scotland CCTV) ──────────────────────────────

FTP_HOST = "ftp.traffic-scotland.co.uk"
FTP_USER = "p93dG2J3M9YI"
FTP_PASS = "ihN45rDz8oz6"
FTP_CAMERA_DIR = Path(__file__).parent / "data" / "cameras"
FTP_CAMERA_DIR.mkdir(parents=True, exist_ok=True)
FTP_FETCH_INTERVAL = 1200  # 20 minutes — cache locally, don't hammer FTP
FTP_CSV_CACHE_TTL = 86400  # Re-fetch CSV once per day


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




# ─── Siphon-First Data Fetchers ──────────────────────────────────────────────
# Pattern: Try Siphon (fast cached REST) first, fall back to legacy direct API calls

def _siphon_severity_to_frontend(severity: str) -> str:
    """Map Siphon severity strings to frontend color codes."""
    mapping = {
        "highest": "red",
        "high": "red",
        "medium": "amber",
        "low": "green",
        "lowest": "green",
        "unknown": "amber",
    }
    return mapping.get((severity or "").lower(), "amber")


async def fetch_tsis_incidents() -> list:
    """Fetch incidents: Siphon PRIMARY, legacy TSIS fallback."""
    data = await siphon_get("/api/traffic/datex2/incidents")
    if data and data.get("incidents"):
        results = []
        for inc in data["incidents"]:
            lat = inc.get("lat")
            lon = inc.get("lon")
            if not lat or not lon:
                continue
            if not in_ayrshire(float(lat), float(lon)):
                continue
            title = inc.get("title") or "Incident"
            if inc.get("road"):
                title = f"{inc['road']}: {title}"
            if inc.get("location_name"):
                title += f" ({inc['location_name']})"
            desc_parts = []
            if inc.get("description"):
                desc_parts.append(inc["description"])
            if inc.get("validity_start"):
                desc_parts.append(f"Since: {inc['validity_start']}")
            if inc.get("validity_end"):
                desc_parts.append(f"Until: {inc['validity_end']}")
            results.append({
                "id": f"siphon-inc-{inc.get('id', '')}",
                "type": "incidents",
                "title": title[:200],
                "description": " | ".join(desc_parts)[:500],
                "lat": float(lat),
                "lon": float(lon),
                "severity": _siphon_severity_to_frontend(inc.get("severity", "")),
                "source": "DATEX II (Siphon)",
                "url": "https://www.traffic.gov.scot/",
                "pub_date": inc.get("validity_start", ""),
                "active": True,
                "datex_type": inc.get("datex_type", ""),
            })
        log.info(f"Siphon incidents: {len(results)} Ayrshire items (from {data.get('count', '?')} total)")
        return results
    # Fallback to legacy
    log.info("Siphon incidents unavailable, falling back to legacy TSIS")
    return await _legacy_fetch_tsis_incidents()


async def fetch_tsis_roadworks() -> list:
    """Fetch roadworks: Siphon PRIMARY, legacy TSIS fallback."""
    data = await siphon_get("/api/traffic/datex2/roadworks")
    if data and data.get("roadworks"):
        results = []
        for rw in data["roadworks"]:
            lat = rw.get("lat")
            lon = rw.get("lon")
            if not lat or not lon:
                continue
            if not in_ayrshire(float(lat), float(lon)):
                continue
            title = "Roadworks"
            if rw.get("road"):
                title = f"{rw['road']} Roadworks"
            if rw.get("location_name"):
                title += f" ({rw['location_name']})"
            if rw.get("planned"):
                title = f"Planned: {title}"
            desc_parts = []
            if rw.get("description"):
                desc_parts.append(rw["description"])
            if rw.get("validity_start"):
                desc_parts.append(f"From: {rw['validity_start']}")
            if rw.get("validity_end"):
                desc_parts.append(f"Until: {rw['validity_end']}")
            results.append({
                "id": f"siphon-rw-{rw.get('id', '')}",
                "type": "roadworks",
                "title": title[:200],
                "description": " | ".join(desc_parts)[:500],
                "lat": float(lat),
                "lon": float(lon),
                "severity": _siphon_severity_to_frontend(rw.get("severity", "medium")),
                "source": "DATEX II (Siphon)",
                "url": "https://www.traffic.gov.scot/",
                "pub_date": rw.get("validity_start", ""),
                "active": not rw.get("planned", False),
                "planned": rw.get("planned", False),
            })
        log.info(f"Siphon roadworks: {len(results)} Ayrshire items (from {data.get('count', '?')} total)")
        return results
    # Fallback to legacy
    log.info("Siphon roadworks unavailable, falling back to legacy TSIS")
    return await _legacy_fetch_tsis_roadworks()


async def fetch_datex2_events() -> list:
    """Fetch DATEX2 incidents: Siphon PRIMARY, legacy DATEX2 XML fallback."""
    data = await siphon_get("/api/traffic/datex2/incidents")
    if data and data.get("incidents"):
        results = []
        for inc in data["incidents"]:
            lat = inc.get("lat")
            lon = inc.get("lon")
            if not lat or not lon:
                continue
            # Wider Ayrshire bbox for DATEX2
            if not (55.15 <= float(lat) <= 55.85 and -5.1 <= float(lon) <= -4.0):
                continue
            title = inc.get("title") or inc.get("datex_type") or "Incident"
            if inc.get("road"):
                title = f"{inc['road']}: {title}"
            if inc.get("location_name"):
                title += f" ({inc['location_name']})"
            desc_parts = []
            if inc.get("description"):
                desc_parts.append(inc["description"])
            if inc.get("validity_start"):
                desc_parts.append(f"Since: {inc['validity_start']}")
            if inc.get("validity_end"):
                desc_parts.append(f"Until: {inc['validity_end']}")
            results.append({
                "id": f"datex-event-{inc.get('id', '')}",
                "type": "incidents",
                "title": title[:200],
                "description": " | ".join(desc_parts)[:500],
                "lat": float(lat),
                "lon": float(lon),
                "severity": _siphon_severity_to_frontend(inc.get("severity", "")),
                "source": "DATEX II",
                "url": "https://www.traffic.gov.scot/",
                "pub_date": inc.get("validity_start", ""),
                "active": True,
                "datex_type": inc.get("datex_type", ""),
                "datex_id": inc.get("id", ""),
            })
        log.info(f"Siphon DATEX2 events: {len(results)} Ayrshire items")
        return results
    log.info("Siphon DATEX2 events unavailable, falling back to legacy XML parsing")
    return await _legacy_fetch_datex2_events()


async def fetch_datex2_roadworks() -> list:
    """Fetch DATEX2 roadworks: Siphon PRIMARY, legacy DATEX2 XML fallback."""
    data = await siphon_get("/api/traffic/datex2/roadworks")
    if data and data.get("roadworks"):
        results = []
        for rw in data["roadworks"]:
            lat = rw.get("lat")
            lon = rw.get("lon")
            if not lat or not lon:
                continue
            if not (55.15 <= float(lat) <= 55.85 and -5.1 <= float(lon) <= -4.0):
                continue
            is_planned = rw.get("planned", False)
            prefix = "Planned: " if is_planned else ""
            title = f"{prefix}Roadworks"
            if rw.get("road"):
                title = f"{prefix}{rw['road']} Roadworks"
            if rw.get("location_name"):
                title += f" ({rw['location_name']})"
            desc_parts = []
            if rw.get("description"):
                desc_parts.append(rw["description"])
            if rw.get("validity_start"):
                desc_parts.append(f"From: {rw['validity_start']}")
            if rw.get("validity_end"):
                desc_parts.append(f"Until: {rw['validity_end']}")
            results.append({
                "id": f"datex-rw-{rw.get('id', '')}",
                "type": "roadworks",
                "title": title[:200],
                "description": " | ".join(desc_parts)[:500],
                "lat": float(lat),
                "lon": float(lon),
                "severity": _siphon_severity_to_frontend(rw.get("severity", "medium")),
                "source": "DATEX II",
                "url": "https://www.traffic.gov.scot/",
                "pub_date": rw.get("validity_start", ""),
                "active": not is_planned,
                "datex_type": rw.get("datex_type", ""),
                "datex_id": rw.get("id", ""),
                "planned": is_planned,
            })
        log.info(f"Siphon DATEX2 roadworks: {len(results)} Ayrshire items")
        return results
    log.info("Siphon DATEX2 roadworks unavailable, falling back to legacy XML parsing")
    return await _legacy_fetch_datex2_roadworks()


async def fetch_traffic_cameras() -> list:
    """Fetch cameras: Siphon PRIMARY, legacy FTP/web fallback."""
    data = await siphon_get("/api/traffic/cameras")
    if data and data.get("cameras"):
        results = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for cam in data["cameras"]:
            lat = cam.get("lat")
            lon = cam.get("lon")
            if not lat or not lon:
                continue
            # Wider Ayrshire filter for cameras
            if not (55.1 <= float(lat) <= 55.9 and -5.1 <= float(lon) <= -4.1):
                continue
            cam_id = cam.get("id", "")
            image_url = cam.get("image_url", "")
            # Check if we have a local FTP copy for this camera
            sensor_id = cam_id.replace(".", "_").replace(".jpg", "")
            local_sensor = _ftp_sensors.get(sensor_id)
            local_health = _ftp_sensor_health.get(sensor_id, {})
            local_img = FTP_CAMERA_DIR / f"{cam_id}.jpg" if cam_id else None
            has_local = local_img and local_img.exists() and local_img.stat().st_size > 500 if local_img else False
            # Prefer local FTP image if available
            if has_local:
                display_url = f"/api/cameras/image/{cam_id}.jpg"
            elif image_url:
                display_url = image_url
            else:
                display_url = ""
            results.append({
                "id": f"sensor-cam-{cam_id}",
                "type": "camera",
                "title": cam.get("name") or cam.get("description") or "CCTV Camera",
                "description": cam.get("description") or "Traffic Scotland CCTV",
                "lat": float(lat),
                "lon": float(lon),
                "severity": "blue",
                "source": "Traffic Scotland (Siphon)",
                "url": display_url,
                "pub_date": now_iso,
                "active": True,
                "camera_url": display_url,
            })
        log.info(f"Siphon cameras: {len(results)} Ayrshire cameras (from {data.get('count', '?')} total)")
        return results
    # Fallback to legacy FTP/web
    log.info("Siphon cameras unavailable, falling back to legacy FTP/web")
    return await _legacy_fetch_traffic_cameras()


# ─── Data Fetchers (Legacy Fallbacks) ─────────────────────────────────────────

async def _legacy_fetch_tsis_incidents() -> list:
    """[LEGACY FALLBACK] Fetch live incidents from Traffic Scotland TSIS JSON API."""
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


async def _legacy_fetch_tsis_roadworks() -> list:
    """[LEGACY FALLBACK] Fetch roadworks from Traffic Scotland TSIS JSON API."""
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
        row = DB.execute("SELECT updated_at FROM cache WHERE key = 'tomtom_flow'").fetchone()
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



async def fetch_tomtom_incidents() -> list:
    """Fetch live traffic incidents from TomTom Incident Details API v5."""
    TOMTOM_INC_TTL = 300  # 5 minutes
    cached = cache_get_stale("tomtom_incidents")
    if cached is not None:
        with db_connect() as conn:
            row = conn.execute("SELECT updated_at FROM cache WHERE key = ?", ("tomtom_incidents",)).fetchone()
        if row and (time.time() - row["updated_at"]) < TOMTOM_INC_TTL:
            return cached

    INCIDENT_CATEGORIES = {
        0: "Unknown",
        1: "Accident",
        2: "Fog",
        3: "Dangerous Conditions",
        4: "Rain",
        5: "Ice",
        6: "Jam",
        7: "Lane Closed",
        8: "Road Closed",
        9: "Roadworks",
        10: "Wind",
        11: "Flooding",
        14: "Broken Vehicle",
    }
    RED_CATS = {1, 6, 8, 11}
    AMBER_CATS = {3, 7, 9, 14}

    url = "https://api.tomtom.com/traffic/services/5/incidentDetails"
    params = {
        "key": TOMTOM_API_KEY,
        "bbox": "-5.1,55.1,-4.1,55.8",
        "language": "en-GB",
        "categoryFilter": "0,1,2,3,4,5,6,7,8,9,10,11,14",
        "t": str(int(time.time())),
    }

    results = []
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                d = resp.json()
                for idx, i in enumerate(d.get("incidents", [])):
                    coords = i.get("geometry", {}).get("coordinates", [])
                    props = i.get("properties", {})
                    category = props.get("iconCategory", 0)
                    magnitude = props.get("magnitudeOfDelay", 0)
                    from_loc = props.get("from", "")
                    to_loc = props.get("to", "")
                    delay = props.get("delay", 0)
                    length = props.get("length", 0)

                    cat_name = INCIDENT_CATEGORIES.get(category, "Unknown")

                    if category in RED_CATS:
                        severity = "red"
                    elif category in AMBER_CATS:
                        severity = "amber"
                    else:
                        severity = "green"

                    # Calculate center from geometry coordinates ([lon, lat] pairs)
                    if coords:
                        center_lon = sum(c[0] for c in coords) / len(coords)
                        center_lat = sum(c[1] for c in coords) / len(coords)
                    else:
                        continue  # skip incidents with no geometry

                    if from_loc and to_loc:
                        title = f"{cat_name}: {from_loc} → {to_loc}"
                    else:
                        title = cat_name

                    desc_parts = []
                    if delay:
                        desc_parts.append(f"Delay: {delay}s")
                    if length:
                        desc_parts.append(f"Length: {length}m")
                    desc_parts.append(f"Magnitude: {magnitude}")
                    description = " | ".join(desc_parts)

                    if category in {1, 6, 8, 11}:
                        item_type = "incidents"
                    elif category in {7, 9, 14}:
                        item_type = "roadworks"
                    else:
                        item_type = "conditions"

                    results.append({
                        "id": f"tt-inc-{idx}",
                        "type": item_type,
                        "title": title,
                        "description": description,
                        "lat": center_lat,
                        "lon": center_lon,
                        "severity": severity,
                        "source": "TomTom",
                        "url": "",
                        "pub_date": datetime.now(timezone.utc).isoformat(),
                        "active": True,
                        "tomtom_geometry": [[lat, lon] for lon, lat in coords],
                    })
            else:
                log.warning(f"TomTom incidents API returned {resp.status_code}")
        except Exception as e:
            log.warning(f"TomTom incidents fetch failed: {e}")

    log.info(f"TomTom incidents: {len(results)} items")
    cache_set("tomtom_incidents", results)
    return results



def osgrid_to_latlon(easting: float, northing: float) -> tuple:
    """Convert OS National Grid (OSGB36) easting/northing to WGS84 lat/lon.
    Uses Helmert transformation via iterative approach."""
    a, b = 6377563.396, 6356256.909  # Airy 1830 ellipsoid
    F0 = 0.9996012717
    lat0 = math.radians(49)
    lon0 = math.radians(-2)
    N0, E0 = -100000, 400000
    e2 = 1 - (b * b) / (a * a)
    n = (a - b) / (a + b)
    n2, n3 = n * n, n * n * n

    lat = lat0
    M = 0
    while True:
        lat = (northing - N0 - M) / (a * F0) + lat
        Ma = (1 + n + (5/4) * n2 + (5/4) * n3) * (lat - lat0)
        Mb = (3 * n + 3 * n2 + (21/8) * n3) * math.sin(lat - lat0) * math.cos(lat + lat0)
        Mc = ((15/8) * n2 + (15/8) * n3) * math.sin(2 * (lat - lat0)) * math.cos(2 * (lat + lat0))
        Md = (35/24) * n3 * math.sin(3 * (lat - lat0)) * math.cos(3 * (lat + lat0))
        M = b * F0 * (Ma - Mb + Mc - Md)
        if abs(northing - N0 - M) < 0.00001:
            break

    cosLat = math.cos(lat)
    sinLat = math.sin(lat)
    nu = a * F0 / math.sqrt(1 - e2 * sinLat * sinLat)
    rho = a * F0 * (1 - e2) / ((1 - e2 * sinLat * sinLat) ** 1.5)
    eta2 = nu / rho - 1
    tanLat = math.tan(lat)

    VII = tanLat / (2 * rho * nu)
    VIII = tanLat / (24 * rho * nu ** 3) * (5 + 3 * tanLat ** 2 + eta2 - 9 * tanLat ** 2 * eta2)
    IX = tanLat / (720 * rho * nu ** 5) * (61 + 90 * tanLat ** 2 + 45 * tanLat ** 4)
    X = 1 / (cosLat * nu)
    XI = 1 / (6 * cosLat * nu ** 3) * (nu / rho + 2 * tanLat ** 2)
    XII = 1 / (120 * cosLat * nu ** 5) * (5 + 28 * tanLat ** 2 + 24 * tanLat ** 4)
    XIIa = 1 / (5040 * cosLat * nu ** 7) * (61 + 662 * tanLat ** 2 + 1320 * tanLat ** 4 + 720 * tanLat ** 6)

    dE = easting - E0
    lat_osgb = lat - VII * dE ** 2 + VIII * dE ** 4 - IX * dE ** 6
    lon_osgb = lon0 + X * dE - XI * dE ** 3 + XII * dE ** 5 - XIIa * dE ** 7

    # Helmert OSGB36 -> WGS84
    tx, ty, tz = 446.448, -125.157, 542.060
    s = -20.4894 / 1e6
    rx = math.radians(0.1502 / 3600)
    ry = math.radians(0.2470 / 3600)
    rz = math.radians(0.8421 / 3600)

    sin_lat = math.sin(lat_osgb)
    cos_lat = math.cos(lat_osgb)
    sin_lon = math.sin(lon_osgb)
    cos_lon = math.cos(lon_osgb)

    nu_os = a / math.sqrt(1 - e2 * sin_lat ** 2)
    x1 = nu_os * cos_lat * cos_lon
    y1 = nu_os * cos_lat * sin_lon
    z1 = nu_os * (1 - e2) * sin_lat

    x2 = tx + (1 + s) * x1 + (-rz) * y1 + (ry) * z1
    y2 = ty + (rz) * x1 + (1 + s) * y1 + (-rx) * z1
    z2 = tz + (-ry) * x1 + (rx) * y1 + (1 + s) * z1

    a_w, b_w = 6378137.0, 6356752.3142  # WGS84 ellipsoid
    e2_w = 1 - (b_w ** 2) / (a_w ** 2)
    p = math.sqrt(x2 ** 2 + y2 ** 2)
    lat_w = math.atan2(z2, p * (1 - e2_w))
    for _ in range(10):
        nu_w = a_w / math.sqrt(1 - e2_w * math.sin(lat_w) ** 2)
        lat_w = math.atan2(z2 + e2_w * nu_w * math.sin(lat_w), p)
    lon_w = math.atan2(y2, x2)

    return round(math.degrees(lat_w), 6), round(math.degrees(lon_w), 6)


# ─── FTP Camera Sensor Network ──────────────────────────────────────────────
# Each camera is a sensor: has an ID, location, health status, last-seen, image freshness

_ftp_sensors = {}       # sensor_id -> {name, image, lat, lon, easting, northing}
_ftp_sensor_health = {} # sensor_id -> {online, last_image_ts, last_check_ts, consecutive_fails}
_ftp_last_csv_fetch = 0
_ftp_last_image_fetch = 0
_ftp_fetch_running = False


def _ftp_connect():
    """Open FTP connection. Caller MUST close with ftp.quit() in finally block."""
    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp


def _fetch_camera_csv():
    """Download and parse cameraimages.csv from FTP (blocking). Populates sensor registry."""
    global _ftp_sensors, _ftp_last_csv_fetch
    ftp = None
    try:
        ftp = _ftp_connect()
        buf = io.BytesIO()
        ftp.retrbinary("RETR cameraimages.csv", buf.write)
        buf.seek(0)
        text = buf.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        sensors = {}
        total_count = 0
        for row in reader:
            total_count += 1
            if len(row) < 4:
                continue
            name, image, easting_s, northing_s = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
            try:
                easting = float(easting_s)
                northing = float(northing_s)
            except (ValueError, TypeError):
                continue
            lat, lon = osgrid_to_latlon(easting, northing)
            # Filter to wider Ayrshire area
            if not (55.1 <= lat <= 55.9 and -5.1 <= lon <= -4.1):
                continue
            sensor_id = image.replace(".jpg", "").replace(".", "_")
            sensors[sensor_id] = {
                "name": name,
                "image": image,
                "lat": lat,
                "lon": lon,
                "easting": easting,
                "northing": northing,
            }
            # Initialise health if new sensor
            if sensor_id not in _ftp_sensor_health:
                _ftp_sensor_health[sensor_id] = {
                    "online": False,
                    "last_image_ts": 0,
                    "last_check_ts": 0,
                    "consecutive_fails": 0,
                    "image_size": 0,
                }
        _ftp_sensors = sensors
        _ftp_last_csv_fetch = time.time()
        log.info(f"FTP sensor registry: {len(sensors)} Ayrshire cameras from {total_count} total")
    except Exception as e:
        log.error(f"FTP CSV fetch failed: {e}")
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


def _fetch_camera_images():
    """Download current camera images from FTP (blocking). Updates sensor health."""
    global _ftp_last_image_fetch, _ftp_fetch_running
    if not _ftp_sensors or _ftp_fetch_running:
        return
    _ftp_fetch_running = True
    ftp = None
    try:
        ftp = _ftp_connect()
        ftp.cwd("/current")
        downloaded = 0
        failed = 0
        now = time.time()
        for sensor_id, sensor in _ftp_sensors.items():
            img_name = sensor["image"]
            local_path = FTP_CAMERA_DIR / img_name
            health = _ftp_sensor_health.get(sensor_id, {})
            try:
                tmp_path = FTP_CAMERA_DIR / f".{img_name}.tmp"
                with open(tmp_path, "wb") as f:
                    ftp.retrbinary(f"RETR {img_name}", f.write)
                file_size = tmp_path.stat().st_size
                if file_size > 500:  # Valid image should be > 500 bytes
                    shutil.move(str(tmp_path), str(local_path))
                    downloaded += 1
                    _ftp_sensor_health[sensor_id] = {
                        "online": True,
                        "last_image_ts": now,
                        "last_check_ts": now,
                        "consecutive_fails": 0,
                        "image_size": file_size,
                    }
                else:
                    tmp_path.unlink(missing_ok=True)
                    failed += 1
                    _ftp_sensor_health[sensor_id] = {
                        **health,
                        "online": False,
                        "last_check_ts": now,
                        "consecutive_fails": health.get("consecutive_fails", 0) + 1,
                    }
            except Exception:
                failed += 1
                _ftp_sensor_health[sensor_id] = {
                    **health,
                    "online": False,
                    "last_check_ts": now,
                    "consecutive_fails": health.get("consecutive_fails", 0) + 1,
                }
                # Clean up tmp file
                tmp = FTP_CAMERA_DIR / f".{img_name}.tmp"
                tmp.unlink(missing_ok=True)
        _ftp_last_image_fetch = now
        log.info(f"FTP sensor images: {downloaded} OK, {failed} failed out of {len(_ftp_sensors)}")
    except Exception as e:
        log.error(f"FTP image fetch failed: {e}")
    finally:
        _ftp_fetch_running = False
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


async def ftp_camera_refresh_loop():
    """Background loop: fetch camera CSV + images from FTP every 20 min."""
    await asyncio.sleep(3)  # Let server start
    loop = asyncio.get_event_loop()
    while True:
        try:
            now = time.time()
            # Refresh CSV once per day
            if now - _ftp_last_csv_fetch > FTP_CSV_CACHE_TTL:
                log.info("FTP sensors: refreshing camera registry CSV...")
                await loop.run_in_executor(None, _fetch_camera_csv)
            # Refresh images every 20 min
            if now - _ftp_last_image_fetch > FTP_FETCH_INTERVAL:
                log.info("FTP sensors: pulling camera images...")
                await loop.run_in_executor(None, _fetch_camera_images)
        except Exception as e:
            log.error(f"FTP sensor loop error: {e}")
        await asyncio.sleep(60)  # Check every minute, only fetch when interval passed


async def _legacy_fetch_traffic_cameras() -> list:
    """[LEGACY FALLBACK] Return camera list from FTP sensor registry (falls back to web API on first boot)."""
    if _ftp_sensors:
        results = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for sensor_id, sensor in _ftp_sensors.items():
            health = _ftp_sensor_health.get(sensor_id, {})
            img_file = FTP_CAMERA_DIR / sensor["image"]
            has_image = img_file.exists() and img_file.stat().st_size > 500
            age_secs = time.time() - health.get("last_image_ts", 0) if health.get("last_image_ts") else None
            # Freshness: green < 25 min, amber < 60 min, red > 60 min / offline
            if has_image and age_secs and age_secs < 1500:
                freshness = "live"
            elif has_image and age_secs and age_secs < 3600:
                freshness = "stale"
            else:
                freshness = "offline"
            results.append({
                "id": f"sensor-cam-{sensor_id}",
                "type": "camera",
                "title": sensor["name"],
                "description": f"Traffic Scotland CCTV — {freshness}",
                "lat": sensor["lat"],
                "lon": sensor["lon"],
                "severity": "blue",
                "source": "Traffic Scotland FTP",
                "url": f"/api/cameras/image/{sensor['image']}" if has_image else "",
                "pub_date": now_iso,
                "active": health.get("online", False),
                "camera_url": f"/api/cameras/image/{sensor['image']}" if has_image else "",
                "sensor_id": sensor_id,
                "sensor_health": {
                    "online": health.get("online", False),
                    "freshness": freshness,
                    "last_image": datetime.fromtimestamp(health["last_image_ts"], tz=timezone.utc).isoformat() if health.get("last_image_ts") else None,
                    "consecutive_fails": health.get("consecutive_fails", 0),
                    "image_size_bytes": health.get("image_size", 0),
                },
            })
        return results

    # Fallback to web API if FTP hasn't loaded yet (first 3 seconds)
    cached = cache_get("traffic_cameras")
    if cached is not None:
        return cached.get("items", [])

    url = "https://www.traffic.gov.scot/tsis/cameras"
    results = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"User-Agent": "AyrTraffic/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("results", data if isinstance(data, list) else [])
                for index, camera in enumerate(items):
                    try:
                        lat = float(camera.get("lat") or 0)
                        lng = float(camera.get("lng") or 0)
                    except (ValueError, TypeError):
                        continue
                    if not lat or not lng:
                        continue
                    if not (55.1 <= lat <= 55.9 and -5.1 <= lng <= -4.1):
                        continue
                    image_url = camera.get("imageUrl") or ""
                    results.append({
                        "id": f"cam-{index}",
                        "type": "camera",
                        "title": camera.get("title") or "CCTV Camera",
                        "description": "Traffic Scotland CCTV",
                        "lat": lat,
                        "lon": lng,
                        "severity": "blue",
                        "source": "Traffic Scotland",
                        "url": image_url,
                        "pub_date": datetime.now(timezone.utc).isoformat(),
                        "active": True,
                        "camera_url": image_url,
                    })
        except Exception as e:
            log.warning(f"Traffic cameras web API fallback failed: {e}")

    log.info(f"Traffic cameras: {len(results)} (web API fallback)")
    cache_set("traffic_cameras", {"items": results})
    return results




# --- CalMac Ferry Routes (Ayrshire) -------------------------------------------

AYRSHIRE_FERRY_ROUTES = {
    "003": {  # Ardrossan - Brodick
        "terminals": [
            {"name": "Ardrossan", "lat": 55.6410, "lon": -4.8120},
            {"name": "Brodick", "lat": 55.5767, "lon": -5.1440},
        ],
    },
    "024": {  # Largs - Cumbrae
        "terminals": [
            {"name": "Largs", "lat": 55.7933, "lon": -4.8675},
            {"name": "Cumbrae Slip", "lat": 55.7700, "lon": -4.9100},
        ],
    },
}

# Troon-Brodick is seasonal and may appear dynamically
TROON_BRODICK_TERMINALS = [
    {"name": "Troon", "lat": 55.5450, "lon": -4.6810},
    {"name": "Brodick", "lat": 55.5767, "lon": -5.1440},
]


async def fetch_ferry_status() -> list:
    """Fetch CalMac ferry status for Ayrshire routes via GraphQL API."""
    cached = cache_get("ferry_status")
    if cached is not None:
        return cached

    query = """{
  routes {
    name
    routeCode
    status
    isStatusChangeUpcoming
    ports {
      name
      portCode
      latitude
      longitude
      order
    }
    routeStatuses {
      id
      title
      status
      subStatus
      detail
      startDateTime
      endDateTime
      updatedAtDateTime
      disruptionReason
    }
  }
}"""

    results = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.post(
                "https://apim.calmac.co.uk/graphql",
                json={"query": query},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "AyrTraffic/1.0",
                },
            )
            if resp.status_code != 200:
                log.warning(f"CalMac API returned {resp.status_code}")
                return cache_get_stale("ferry_status") or []

            data = resp.json()
            routes = data.get("data", {}).get("routes", [])

            for route in routes:
                code = route.get("routeCode", "")
                name = route.get("name", "")
                overall_status = route.get("status", "NORMAL")

                # Check if this is an Ayrshire route
                is_ayrshire = code in AYRSHIRE_FERRY_ROUTES

                # Also detect Troon-Brodick (seasonal) by name
                is_troon = "troon" in name.lower() and "brodick" in name.lower()

                if not is_ayrshire and not is_troon:
                    continue

                # Determine severity from overall status
                status_map = {
                    "NORMAL": "green",
                    "WARNING": "amber",
                    "DISRUPTED": "red",
                    "CANCELLED": "red",
                    "INFORMATION": "green",
                    "SERVICE": "green",
                    "SAILING": "green",
                }
                severity = status_map.get(overall_status, "amber")

                # Check routeStatuses for active disruptions
                now_str = datetime.now(timezone.utc).isoformat()
                active_disruptions = []
                for rs in route.get("routeStatuses", []):
                    rs_status = rs.get("status", "")
                    rs_sub = rs.get("subStatus", "")

                    # Skip purely informational statuses
                    if rs_status in ("INFORMATION", "SERVICE"):
                        continue

                    # Check if status is currently active
                    start = rs.get("startDateTime", "")
                    end = rs.get("endDateTime", "")
                    if end and end < now_str:
                        continue
                    if start and start > now_str:
                        continue

                    active_disruptions.append(rs)

                    # Upgrade severity based on disruption type
                    if rs_status == "SAILING" and rs_sub in ("DISRUPTIONS", "CANCELLED"):
                        severity = "red"
                    elif rs_status == "SAILING" and rs_sub == "BE_AWARE":
                        if severity != "red":
                            severity = "amber"
                    elif rs_status == "WARNING":
                        if severity != "red":
                            severity = "amber"
                    elif rs_status == "DISRUPTION" or rs_sub == "CANCELLED":
                        severity = "red"

                # Build description from active disruptions
                desc_parts = []
                for d in active_disruptions[:3]:
                    title = d.get("title", "")
                    detail = d.get("detail", "")
                    reason = d.get("disruptionReason", "")
                    # Strip markdown links and keep text short
                    detail_clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", detail)
                    detail_clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", detail_clean)
                    detail_clean = detail_clean.strip()[:200]
                    if reason:
                        desc_parts.append(f"{title} ({reason}): {detail_clean}")
                    else:
                        desc_parts.append(f"{title}: {detail_clean}")

                description = " | ".join(desc_parts) if desc_parts else f"Service {overall_status.lower()}"

                # Status label
                status_label = {
                    "green": "Normal Service",
                    "amber": "Disrupted",
                    "red": "Cancelled/Major Disruption",
                }.get(severity, overall_status)

                # Get terminal coordinates
                if is_troon and not is_ayrshire:
                    terminals = TROON_BRODICK_TERMINALS
                else:
                    terminals = AYRSHIRE_FERRY_ROUTES[code]["terminals"]

                # Create an item per terminal
                for terminal in terminals:
                    results.append({
                        "id": f"ferry-{code}-{terminal['name'].lower().replace(' ', '-')}",
                        "type": "ferry",
                        "title": f"{name}: {status_label}",
                        "description": description,
                        "lat": terminal["lat"],
                        "lon": terminal["lon"],
                        "severity": severity,
                        "source": "CalMac",
                        "url": "https://www.calmac.co.uk/service-status",
                        "pub_date": datetime.now(timezone.utc).isoformat(),
                        "active": True,
                        "ferry_data": {
                            "route_code": code,
                            "route_name": name,
                            "terminal": terminal["name"],
                            "overall_status": overall_status,
                            "disruption_count": len(active_disruptions),
                        },
                    })

        except Exception as e:
            log.warning(f"CalMac ferry fetch failed: {e}")
            stale = cache_get_stale("ferry_status")
            if stale:
                return stale

    log.info(f"CalMac ferries: {len(results)} terminal markers from {len(results)//2 if results else 0} routes")
    cache_set("ferry_status", results)
    return results




# ─── DATEX II Fetchers (Traffic Scotland) ─────────────────────────────────────

def _datex2_extract_coords(group_of_locations) -> list:
    """Extract all lat/lon pairs from a DATEX II groupOfLocations element."""
    ns = DATEX2_NS
    coords = []
    for pc in group_of_locations.iter("{http://datex2.eu/schema/2/2_0}pointCoordinates"):
        lat_el = pc.find("{http://datex2.eu/schema/2/2_0}latitude")
        lon_el = pc.find("{http://datex2.eu/schema/2/2_0}longitude")
        if lat_el is not None and lon_el is not None:
            try:
                coords.append((float(lat_el.text), float(lon_el.text)))
            except (ValueError, TypeError):
                pass
    return coords


def _datex2_get_text(element, tag_path: str) -> str:
    """Get text from a nested DATEX II element path using namespace."""
    ns = "http://datex2.eu/schema/2/2_0"
    current = element
    for tag in tag_path.split("/"):
        current = current.find(f"{{{ns}}}{tag}")
        if current is None:
            return ""
    return (current.text or "").strip()


def _datex2_get_comment(record) -> str:
    """Extract generalPublicComment text from a situation record."""
    ns = "http://datex2.eu/schema/2/2_0"
    for comment in record.iter(f"{{{ns}}}generalPublicComment"):
        for value in comment.iter(f"{{{ns}}}value"):
            if value.text:
                return value.text.strip()
    return ""


def _datex2_get_road_name(group_of_locations) -> str:
    """Extract road name (e.g. 'M77', 'A77') from location descriptors."""
    ns = "http://datex2.eu/schema/2/2_0"
    for desc in group_of_locations.iter(f"{{{ns}}}descriptor"):
        for value in desc.iter(f"{{{ns}}}value"):
            text = (value.text or "").strip()
            if re.match(r"^[AM]\d+", text):
                return text
    # Try ilc names
    for ilc in group_of_locations.iter(f"{{{ns}}}ilc"):
        for value in ilc.iter(f"{{{ns}}}value"):
            text = (value.text or "").strip()
            if re.match(r"^[AM]\d+", text):
                return text
    return ""


def _datex2_get_location_name(group_of_locations) -> str:
    """Extract human-readable location name from otherName or name descriptors."""
    ns = "http://datex2.eu/schema/2/2_0"
    names = []
    for other in group_of_locations.iter(f"{{{ns}}}otherName"):
        for value in other.iter(f"{{{ns}}}value"):
            text = (value.text or "").strip()
            if text and text not in names:
                names.append(text)
    if names:
        return " to ".join(names[:2])
    # Fallback to any name descriptor
    for name_el in group_of_locations.iter(f"{{{ns}}}name"):
        for value in name_el.iter(f"{{{ns}}}value"):
            text = (value.text or "").strip()
            if text and len(text) > 3:
                return text
    return ""


async def fetch_datex2_xml(publication: str) -> Optional[ET.Element]:
    """Fetch and parse a DATEX II publication XML, with caching."""
    cache_key = f"datex2_raw_{publication}"
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return ET.fromstring(cached["xml"])
        except Exception:
            pass

    url = f"{DATEX2_BASE_URL}/{publication}/Content.xml"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, auth=DATEX2_AUTH)
            if resp.status_code == 200:
                xml_text = resp.text
                root = ET.fromstring(xml_text)
                # Cache the raw XML string
                cache_set(cache_key, {"xml": xml_text})
                return root
            else:
                log.warning(f"DATEX II {publication}: HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"DATEX II {publication} fetch failed: {e}")

    # Fallback to stale cache
    stale = cache_get_stale(cache_key)
    if stale:
        try:
            return ET.fromstring(stale["xml"])
        except Exception:
            pass
    return None


async def _legacy_fetch_datex2_events() -> list:
    """[LEGACY FALLBACK] Fetch unplanned events (accidents, breakdowns, etc.) from DATEX II."""
    root = await fetch_datex2_xml("UnplannedEvents")
    if root is None:
        return []

    ns = "http://datex2.eu/schema/2/2_0"
    results = []

    for situation in root.iter(f"{{{ns}}}situation"):
        sit_id = situation.get("id", "")
        for record in situation.iter(f"{{{ns}}}situationRecord"):
            rec_id = record.get("id", "")
            # Get xsi:type via attribute with full namespace
            xsi_type = record.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")

            # Extract coordinates
            coords = []
            for gol in record.iter(f"{{{ns}}}groupOfLocations"):
                coords = _datex2_extract_coords(gol)
                road_name = _datex2_get_road_name(gol)
                location_name = _datex2_get_location_name(gol)
                break

            if not coords:
                continue

            # Use midpoint of all coordinates
            avg_lat = sum(c[0] for c in coords) / len(coords)
            avg_lon = sum(c[1] for c in coords) / len(coords)

            # Filter to Ayrshire area (slightly wider for DATEX)
            if not (55.15 <= avg_lat <= 55.85 and -5.1 <= avg_lon <= -4.0):
                continue

            comment = _datex2_get_comment(record)

            # Get validity times
            start_time = ""
            end_time = ""
            for validity in record.iter(f"{{{ns}}}validityTimeSpecification"):
                st = validity.find(f"{{{ns}}}overallStartTime")
                et = validity.find(f"{{{ns}}}overallEndTime")
                if st is not None:
                    start_time = st.text or ""
                if et is not None:
                    end_time = et.text or ""

            # Determine severity from impact
            severity = "amber"
            for impact in record.iter(f"{{{ns}}}impact"):
                lanes = impact.find(f"{{{ns}}}numberOfLanesRestricted")
                if lanes is not None:
                    try:
                        if int(lanes.text) >= 2:
                            severity = "red"
                    except (ValueError, TypeError):
                        pass
                for delays in impact.iter(f"{{{ns}}}delays"):
                    dt = delays.find(f"{{{ns}}}delaysType")
                    if dt is not None and dt.text in ("longDelays", "veryLongDelays"):
                        severity = "red"
                    elif dt is not None and dt.text == "delays":
                        severity = "amber"

            # Map xsi:type to friendly names
            type_map = {
                "Accident": "Accident",
                "AbnormalTraffic": "Abnormal Traffic",
                "VehicleObstruction": "Vehicle Obstruction",
                "AnimalPresenceObstruction": "Animal on Road",
                "GeneralObstruction": "Obstruction",
                "EnvironmentalObstruction": "Environmental Hazard",
                "InfrastructureDamageObstruction": "Infrastructure Damage",
                "ReroutingManagement": "Rerouting",
                "SpeedManagement": "Speed Restriction",
                "RoadOrCarriagewayOrLaneManagement": "Lane Management",
            }
            friendly_type = type_map.get(xsi_type, xsi_type or "Incident")

            if xsi_type in ("Accident", "VehicleObstruction"):
                severity = "red"

            title = f"{friendly_type}"
            if road_name:
                title = f"{road_name}: {friendly_type}"
            if location_name:
                title += f" ({location_name})"

            desc_parts = []
            if comment:
                desc_parts.append(comment)
            if start_time:
                desc_parts.append(f"Since: {start_time}")
            if end_time:
                desc_parts.append(f"Until: {end_time}")

            results.append({
                "id": f"datex-event-{rec_id}",
                "type": "incidents",
                "title": title[:200],
                "description": " | ".join(desc_parts)[:500],
                "lat": avg_lat,
                "lon": avg_lon,
                "severity": severity,
                "source": "DATEX II",
                "url": "https://www.traffic.gov.scot/",
                "pub_date": start_time,
                "active": True,
                "datex_type": xsi_type,
                "datex_id": rec_id,
            })

    log.info(f"DATEX II UnplannedEvents: {len(results)} Ayrshire items")
    return results


async def _legacy_fetch_datex2_roadworks() -> list:
    """[LEGACY FALLBACK] Fetch current and future roadworks from DATEX II."""
    results = []

    for pub_name in ("CurrentRoadworks", "FutureRoadworks"):
        root = await fetch_datex2_xml(pub_name)
        if root is None:
            continue

        ns = "http://datex2.eu/schema/2/2_0"
        is_future = pub_name == "FutureRoadworks"

        for situation in root.iter(f"{{{ns}}}situation"):
            for record in situation.iter(f"{{{ns}}}situationRecord"):
                rec_id = record.get("id", "")
                xsi_type = record.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")

                coords = []
                road_name = ""
                location_name = ""
                for gol in record.iter(f"{{{ns}}}groupOfLocations"):
                    coords = _datex2_extract_coords(gol)
                    road_name = _datex2_get_road_name(gol)
                    location_name = _datex2_get_location_name(gol)
                    break

                if not coords:
                    continue

                avg_lat = sum(c[0] for c in coords) / len(coords)
                avg_lon = sum(c[1] for c in coords) / len(coords)

                if not (55.15 <= avg_lat <= 55.85 and -5.1 <= avg_lon <= -4.0):
                    continue

                comment = _datex2_get_comment(record)

                start_time = ""
                end_time = ""
                for validity in record.iter(f"{{{ns}}}validityTimeSpecification"):
                    st = validity.find(f"{{{ns}}}overallStartTime")
                    et = validity.find(f"{{{ns}}}overallEndTime")
                    if st is not None:
                        start_time = st.text or ""
                    if et is not None:
                        end_time = et.text or ""

                # Check for impact/delays
                severity = "amber"
                for impact in record.iter(f"{{{ns}}}impact"):
                    for delays in impact.iter(f"{{{ns}}}delays"):
                        dt = delays.find(f"{{{ns}}}delaysType")
                        if dt is not None and dt.text in ("longDelays", "veryLongDelays"):
                            severity = "red"

                prefix = "Planned: " if is_future else ""
                title = f"{prefix}Roadworks"
                if road_name:
                    title = f"{prefix}{road_name} Roadworks"
                if location_name:
                    title += f" ({location_name})"

                desc_parts = []
                if comment:
                    desc_parts.append(comment)
                if start_time:
                    desc_parts.append(f"From: {start_time}")
                if end_time:
                    desc_parts.append(f"Until: {end_time}")

                results.append({
                    "id": f"datex-rw-{rec_id}",
                    "type": "roadworks",
                    "title": title[:200],
                    "description": " | ".join(desc_parts)[:500],
                    "lat": avg_lat,
                    "lon": avg_lon,
                    "severity": severity,
                    "source": "DATEX II",
                    "url": "https://www.traffic.gov.scot/",
                    "pub_date": start_time,
                    "active": not is_future,
                    "datex_type": xsi_type,
                    "datex_id": rec_id,
                    "planned": is_future,
                })

    log.info(f"DATEX II Roadworks: {len(results)} Ayrshire items")
    return results


async def fetch_datex2_vms() -> list:
    """Fetch VMS (Variable Message Signs) data with locations from VMSTable."""
    # First get the VMS table for locations
    vms_table_root = await fetch_datex2_xml("VMSTable")
    vms_locations = {}
    if vms_table_root is not None:
        ns = "http://datex2.eu/schema/2/2_0"
        for record in vms_table_root.iter(f"{{{ns}}}vmsUnitRecord"):
            vms_id = record.get("id", "")
            coords = _datex2_extract_coords(record)
            name_parts = []
            for name_el in record.iter(f"{{{ns}}}value"):
                text = (name_el.text or "").strip()
                if text and len(text) > 2:
                    name_parts.append(text)
            if coords:
                vms_locations[vms_id] = {
                    "lat": coords[0][0],
                    "lon": coords[0][1],
                    "name": name_parts[0] if name_parts else vms_id,
                }

    # Now get live VMS messages
    root = await fetch_datex2_xml("VMS")
    if root is None:
        return []

    ns = "http://datex2.eu/schema/2/2_0"
    results = []

    for vms_unit in root.iter(f"{{{ns}}}vmsUnit"):
        # Get VMS unit reference ID
        ref_el = vms_unit.find(f"{{{ns}}}vmsUnitReference")
        vms_id = ref_el.get("id", "") if ref_el is not None else ""

        loc = vms_locations.get(vms_id)
        if not loc:
            continue

        # Filter to Ayrshire area
        if not (55.15 <= loc["lat"] <= 55.85 and -5.1 <= loc["lon"] <= -4.0):
            continue

        # Extract message text
        messages = []
        for text_line in vms_unit.iter(f"{{{ns}}}vmsTextLine"):
            # The innermost vmsTextLine contains the actual text
            text_el = text_line.find(f"{{{ns}}}vmsTextLine")
            if text_el is not None and text_el.text:
                messages.append(text_el.text.strip())

        # Get time last set
        time_set = ""
        for tls in vms_unit.iter(f"{{{ns}}}timeLastSet"):
            if tls.text:
                time_set = tls.text.strip()
                break

        # Is VMS working?
        working = True
        for w in vms_unit.iter(f"{{{ns}}}vmsWorking"):
            working = w.text.lower() == "true" if w.text else True
            break

        if not messages:
            continue

        message_text = " | ".join(m for m in messages if m.strip())

        results.append({
            "id": f"datex-vms-{vms_id}",
            "type": "vms",
            "title": f"VMS: {loc['name']}",
            "description": message_text,
            "lat": loc["lat"],
            "lon": loc["lon"],
            "severity": "blue",
            "source": "DATEX II",
            "url": "https://www.traffic.gov.scot/",
            "pub_date": time_set,
            "active": working,
            "vms_data": {
                "vms_id": vms_id,
                "location_name": loc["name"],
                "message": message_text,
                "time_set": time_set,
                "working": working,
            },
        })

    log.info(f"DATEX II VMS: {len(results)} Ayrshire signs")
    return results


async def fetch_datex2_travel_times() -> list:
    """Fetch travel time data from DATEX II with site locations."""
    # First get site definitions
    sites_root = await fetch_datex2_xml("TravelTimeSites")
    site_info = {}
    if sites_root is not None:
        ns = "http://datex2.eu/schema/2/2_0"
        for record in sites_root.iter(f"{{{ns}}}measurementSiteRecord"):
            site_id = record.get("id", "")
            name = ""
            for name_el in record.iter(f"{{{ns}}}measurementSiteName"):
                for v in name_el.iter(f"{{{ns}}}value"):
                    if v.text:
                        name = v.text.strip()
                        break
                break
            coords = _datex2_extract_coords(record)
            if coords:
                avg_lat = sum(c[0] for c in coords) / len(coords)
                avg_lon = sum(c[1] for c in coords) / len(coords)
                site_info[site_id] = {"name": name, "lat": avg_lat, "lon": avg_lon}

    # Now get live travel time data
    root = await fetch_datex2_xml("TravelTimeData")
    if root is None:
        return []

    ns = "http://datex2.eu/schema/2/2_0"
    results = []

    for sm in root.iter(f"{{{ns}}}siteMeasurements"):
        ref = sm.find(f"{{{ns}}}measurementSiteReference")
        site_id = ref.get("id", "") if ref is not None else ""

        site = site_info.get(site_id)
        if not site:
            continue

        # Filter to Ayrshire area
        if not (55.15 <= site["lat"] <= 55.85 and -5.1 <= site["lon"] <= -4.0):
            continue

        # Extract travel time data
        travel_time = None
        free_flow_time = None
        normal_time = None
        free_flow_speed = None
        measurement_time = ""

        mt = sm.find(f"{{{ns}}}measurementTimeDefault")
        if mt is not None and mt.text:
            measurement_time = mt.text.strip()

        for bd in sm.iter(f"{{{ns}}}basicData"):
            for tt in bd.iter(f"{{{ns}}}travelTime"):
                dur = tt.find(f"{{{ns}}}duration")
                if dur is not None and dur.text:
                    travel_time = int(dur.text)
                break
            for fft in bd.iter(f"{{{ns}}}freeFlowTravelTime"):
                dur = fft.find(f"{{{ns}}}duration")
                if dur is not None and dur.text:
                    free_flow_time = int(dur.text)
                break
            for nt in bd.iter(f"{{{ns}}}normallyExpectedTravelTime"):
                dur = nt.find(f"{{{ns}}}duration")
                if dur is not None and dur.text:
                    normal_time = int(dur.text)
                break
            for ffs in bd.iter(f"{{{ns}}}freeFlowSpeed"):
                spd = ffs.find(f"{{{ns}}}speed")
                if spd is not None and spd.text:
                    free_flow_speed = float(spd.text)
                break

        if travel_time is None:
            continue

        # Calculate congestion ratio
        congestion = 0
        if free_flow_time and free_flow_time > 0:
            congestion = max(0, (travel_time - free_flow_time) / free_flow_time)

        if congestion > 0.5:
            severity = "red"
            status = "Heavy Delays"
        elif congestion > 0.2:
            severity = "amber"
            status = "Moderate Delays"
        else:
            severity = "green"
            status = "Normal"

        tt_mins = round(travel_time / 60, 1)
        ff_mins = round(free_flow_time / 60, 1) if free_flow_time else "?"

        results.append({
            "id": f"datex-tt-{site_id}",
            "type": "travel_time",
            "title": f"{site['name']}: {status}",
            "description": f"Travel time: {tt_mins} min (free flow: {ff_mins} min) | Delay ratio: {congestion:.0%}",
            "lat": site["lat"],
            "lon": site["lon"],
            "severity": severity,
            "source": "DATEX II",
            "url": "https://www.traffic.gov.scot/",
            "pub_date": measurement_time,
            "active": True,
            "travel_time_data": {
                "site_id": site_id,
                "site_name": site["name"],
                "travel_time_seconds": travel_time,
                "free_flow_seconds": free_flow_time,
                "normal_seconds": normal_time,
                "free_flow_speed_kmh": free_flow_speed,
                "congestion_ratio": round(congestion, 3),
                "status": status,
                "measured_at": measurement_time,
            },
        })

    log.info(f"DATEX II TravelTimes: {len(results)} Ayrshire segments")
    return results


async def fetch_all_datex2() -> dict:
    """Fetch all DATEX II data sources and return structured result."""
    events, roadworks, vms, travel_times = await asyncio.gather(
        fetch_datex2_events(),
        fetch_datex2_roadworks(),
        fetch_datex2_vms(),
        fetch_datex2_travel_times(),
        return_exceptions=True,
    )
    events = events if isinstance(events, list) else []
    roadworks = roadworks if isinstance(roadworks, list) else []
    vms = vms if isinstance(vms, list) else []
    travel_times = travel_times if isinstance(travel_times, list) else []

    return {
        "events": events,
        "roadworks": roadworks,
        "vms": vms,
        "travel_times": travel_times,
        "meta": {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_events": len(events),
            "total_roadworks": len(roadworks),
            "total_vms": len(vms),
            "total_travel_times": len(travel_times),
        },
    }


async def fetch_all_data() -> dict:
    """Fetch all data sources concurrently."""
    ts_incidents, ts_roadworks, osm, srwr, flow, tros, tt_incidents, ferries, cameras, datex2_data = await asyncio.gather(
        fetch_tsis_incidents(),
        fetch_tsis_roadworks(),
        fetch_overpass_incidents(),
        fetch_srwr_roadworks(),
        generate_flow_data(),
        fetch_ara_tros(),
        fetch_tomtom_incidents(),
        fetch_ferry_status(),
        fetch_traffic_cameras(),
        fetch_all_datex2(),
        return_exceptions=True,
    )

    all_items = []
    for result in [ts_incidents, ts_roadworks, osm, srwr, flow, tros, tt_incidents, ferries, cameras]:
        if isinstance(result, list):
            all_items.extend(result)

    # Add DATEX II items
    if isinstance(datex2_data, dict):
        all_items.extend(datex2_data.get("events", []))
        all_items.extend(datex2_data.get("roadworks", []))
        all_items.extend(datex2_data.get("vms", []))
        all_items.extend(datex2_data.get("travel_times", []))

    incidents = [i for i in all_items if i["type"] == "incidents"]
    roadworks = [i for i in all_items if i["type"] in ("roadworks", "conditions")]
    tros = [i for i in all_items if i["type"] == "tro"]
    flow_items = [i for i in all_items if i["type"] == "flow"]
    ferry_items = [i for i in all_items if i["type"] == "ferry"]
    camera_items = [i for i in all_items if i["type"] == "camera"]
    vms_items = [i for i in all_items if i["type"] == "vms"]
    travel_time_items = [i for i in all_items if i["type"] == "travel_time"]

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
        "ferries": ferry_items,
        "vms": vms_items,
        "travel_times": travel_time_items,
        "all": deduplicate(all_items),
        "meta": {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_incidents": len(incidents),
            "total_roadworks": len(roadworks),
            "total_tros": len(tros),
            "total_ferries": len(ferry_items),
            "total_cameras": len(camera_items),
            "total_vms": len(vms_items),
            "total_travel_times": len(travel_time_items),
            "total_tomtom_incidents": len(tt_incidents) if isinstance(tt_incidents, list) else 0,
            "total_datex2_events": len(datex2_data.get("events", [])) if isinstance(datex2_data, dict) else 0,
            "total_datex2_roadworks": len(datex2_data.get("roadworks", [])) if isinstance(datex2_data, dict) else 0,
            "sources_active": sum([
                1 if ts_incidents and not isinstance(ts_incidents, Exception) else 0,
                1 if ts_roadworks and not isinstance(ts_roadworks, Exception) else 0,
                1,  # OSM / Overpass
                1,  # Flow
                1 if tros and not isinstance(tros, Exception) else 0,  # ARA + Transport Scotland TROs
                1 if ferries and not isinstance(ferries, Exception) else 0,  # CalMac ferries
                1 if isinstance(datex2_data, dict) else 0,  # DATEX II
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
                            "camera_sensors": len(_ftp_sensors),
                            "camera_sensors_online": sum(1 for h in _ftp_sensor_health.values() if h.get("online")),
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
    asyncio.create_task(ftp_camera_refresh_loop())
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




@app.get("/api/traffic/ferries")
async def get_ferries():
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse({"items": data.get("ferries", []), "meta": data.get("meta", {})})

@app.get("/api/traffic/flow")
async def get_flow():
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse({"items": data.get("flow", []), "meta": data.get("meta", {})})


@app.get("/api/nuro")
async def nuro():
    """Compact traffic summary for nuro dashboard — v2.0 streams format."""
    data = cache_get("all_traffic") or await fetch_all_data()
    meta = data.get("meta", {})
    flow_items = data.get("flow", [])

    # Calculate average congestion from flow data
    if flow_items:
        avg_congestion = sum(f.get("flow_data", {}).get("congestion_level", 0) for f in flow_items) / len(flow_items)
        avg_speed = sum(f.get("flow_data", {}).get("current_speed", 0) for f in flow_items) / len(flow_items)
    else:
        avg_congestion = 0
        avg_speed = 0

    congestion_pct = round(avg_congestion * 100)

    # Count active jams/incidents by severity
    all_items = data.get("all", [])
    red_count = sum(1 for i in all_items if i.get("severity") == "red" and i.get("type") in ("incidents", "conditions"))
    amber_count = sum(1 for i in all_items if i.get("severity") == "amber" and i.get("type") in ("incidents", "conditions", "roadworks"))

    # Congestion status
    if congestion_pct > 50:
        status_color = "#ef4444"
        status_label = "Heavy"
    elif congestion_pct > 30:
        status_color = "#f59e0b"
        status_label = "Moderate"
    else:
        status_color = "#22c55e"
        status_label = "Clear"

    streams = [
        {
            "id": "congestion",
            "label": "Avg Congestion",
            "type": "gauge",
            "value": congestion_pct,
            "unit": "%",
            "min": 0,
            "max": 100,
            "color": status_color,
        },
        {
            "id": "avg_speed",
            "label": "Avg Speed",
            "type": "stat",
            "value": round(avg_speed, 1),
            "unit": "km/h",
            "color": status_color,
        },
        {
            "id": "active_incidents",
            "label": "Active Incidents",
            "type": "stat",
            "value": red_count,
            "unit": "red",
            "color": "#ef4444" if red_count > 0 else "#22c55e",
        },
        {
            "id": "roadworks",
            "label": "Roadworks",
            "type": "stat",
            "value": meta.get("total_roadworks", 0),
            "unit": "active",
            "color": "#f59e0b",
        },
        {
            "id": "camera_sensors",
            "label": "CCTV Sensors",
            "type": "sensor_grid",
            "value": len(_ftp_sensors),
            "unit": "cameras",
            "color": "#3b82f6",
            "detail": {
                "source": "traffic_scotland_ftp",
                "online": sum(1 for h in _ftp_sensor_health.values() if h.get("online")),
                "offline": sum(1 for h in _ftp_sensor_health.values() if not h.get("online")),
                "health_pct": round(sum(1 for h in _ftp_sensor_health.values() if h.get("online")) / max(len(_ftp_sensors), 1) * 100),
                "fetch_interval": FTP_FETCH_INTERVAL,
                "last_fetch": datetime.fromtimestamp(_ftp_last_image_fetch, tz=timezone.utc).isoformat() if _ftp_last_image_fetch else None,
            },
        },
        {
            "id": "ferries",
            "label": "Ferries",
            "type": "stat",
            "value": meta.get("total_ferries", 0),
            "unit": "routes",
            "color": "#06b6d4",
        },
    ]

    return JSONResponse({
        "version": "2.0",
        "label": "AyrTraffic",
        "icon": "\U0001f6a6",
        "description": f"Ayrshire traffic \u2014 {status_label} ({congestion_pct}% avg congestion)",
        "streams": streams,
        "updated_at": meta.get("updated_at", datetime.now(timezone.utc).isoformat()),
    })


@app.get("/api/traffic/congestion-zones")
async def congestion_zones():
    """Return congestion zones with geometry for cross-app consumption."""
    data = cache_get("all_traffic") or await fetch_all_data()
    flow_items = data.get("flow", [])
    all_items = data.get("all", [])

    zones = []

    # Flow segments with congestion > 0.2
    for item in flow_items:
        fd = item.get("flow_data", {})
        congestion = fd.get("congestion_level", 0)
        if congestion >= 0.2:
            zones.append({
                "id": item.get("id"),
                "type": "flow",
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "congestion": congestion,
                "current_speed": fd.get("current_speed", 0),
                "free_flow_speed": fd.get("free_flow_speed", 0),
                "status": fd.get("status", "Unknown"),
                "route": fd.get("route", ""),
                "geometry": fd.get("geometry", []),
            })

    # TomTom incidents (jams, accidents) with geometry
    for item in all_items:
        if item.get("source") == "TomTom" and item.get("type") in ("incidents", "conditions"):
            geom = item.get("tomtom_geometry", [])
            if geom:
                zones.append({
                    "id": item.get("id"),
                    "type": "incident",
                    "lat": item.get("lat"),
                    "lon": item.get("lon"),
                    "congestion": 0.8 if item.get("severity") == "red" else 0.5,
                    "title": item.get("title", ""),
                    "severity": item.get("severity", "amber"),
                    "geometry": geom,
                })

    # DATEX II incidents and roadworks as congestion zones
    for item in all_items:
        if item.get("source") == "DATEX II" and item.get("type") in ("incidents", "roadworks"):
            sev = item.get("severity", "amber")
            congestion_val = 0.9 if sev == "red" else 0.5 if sev == "amber" else 0.2
            zones.append({
                "id": item.get("id"),
                "type": "datex_incident" if item.get("type") == "incidents" else "datex_roadworks",
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "congestion": congestion_val,
                "title": item.get("title", ""),
                "severity": sev,
                "source": "DATEX II",
                "geometry": [],
            })

    return JSONResponse({
        "zones": zones,
        "count": len(zones),
        "updated_at": data.get("meta", {}).get("updated_at"),
    })


@app.get("/api/datex")
async def get_datex():
    """Return all parsed DATEX II data in clean JSON format."""
    # Check if we have cached datex2 data
    cached = cache_get("datex2_all")
    if cached is not None:
        return JSONResponse(cached)
    datex_data = await fetch_all_datex2()
    cache_set("datex2_all", datex_data)
    return JSONResponse(datex_data)


@app.get("/api/cameras")
async def get_cameras():
    data = cache_get("all_traffic") or await fetch_all_data()
    cameras = [i for i in data.get("all", []) if i.get("type") == "camera"]
    return JSONResponse({"items": cameras, "count": len(cameras)})


@app.get("/api/cameras/image/{filename}")
async def get_camera_image(filename: str):
    """Serve a locally-cached FTP camera image."""
    safe_name = Path(filename).name
    if not safe_name.endswith(".jpg"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    img_path = FTP_CAMERA_DIR / safe_name
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Camera image not found")
    return FileResponse(
        str(img_path),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Sensor-Updated": datetime.fromtimestamp(
                img_path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        },
    )


@app.get("/api/cameras/sensors")
async def camera_sensors():
    """Full sensor network status — nuro-style sensor report."""
    now = time.time()
    sensors_out = []
    online = 0
    stale = 0
    offline = 0
    for sensor_id, sensor in _ftp_sensors.items():
        health = _ftp_sensor_health.get(sensor_id, {})
        age = now - health.get("last_image_ts", 0) if health.get("last_image_ts") else None
        if health.get("online") and age and age < 1500:
            status = "live"
            online += 1
        elif health.get("online") and age and age < 3600:
            status = "stale"
            stale += 1
        else:
            status = "offline"
            offline += 1
        sensors_out.append({
            "sensor_id": sensor_id,
            "region_id": sensor_id.split("_", 1)[0] if "_" in sensor_id else None,
            "name": sensor["name"],
            "lat": sensor["lat"],
            "lon": sensor["lon"],
            "status": status,
            "last_image_age_seconds": round(age) if age else None,
            "consecutive_fails": health.get("consecutive_fails", 0),
            "image_url": f"/api/cameras/image/{sensor['image']}",
        })
    return JSONResponse({
        "source": "traffic_scotland_ftp",
        "total_sensors": len(_ftp_sensors),
        "online": online,
        "stale": stale,
        "offline": offline,
        "health_pct": round(online / max(len(_ftp_sensors), 1) * 100),
        "last_csv_fetch": datetime.fromtimestamp(_ftp_last_csv_fetch, tz=timezone.utc).isoformat() if _ftp_last_csv_fetch else None,
        "last_image_fetch": datetime.fromtimestamp(_ftp_last_image_fetch, tz=timezone.utc).isoformat() if _ftp_last_image_fetch else None,
        "fetch_interval_seconds": FTP_FETCH_INTERVAL,
        "sensors": sensors_out,
    })



# ─── Vision Analysis Proxy ────────────────────────────────────────────────────

VISION_SERVICE = "http://localhost:8882"

@app.get("/api/vision")
async def vision_proxy():
    """Proxy full vision analysis from camera-vision service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE}/api/analysis")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        log.warning(f"Vision service unavailable: {e}")
        return JSONResponse({"error": "Vision service unavailable"}, status_code=503)


@app.get("/api/vision/summary")
async def vision_summary_proxy():
    """Proxy vision summary from camera-vision service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE}/api/analysis/summary")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        log.warning(f"Vision service unavailable: {e}")
        return JSONResponse({"error": "Vision service unavailable"}, status_code=503)




@app.get("/api/vision/alerts")
async def vision_alerts_proxy():
    """Proxy vision alerts from camera-vision service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE}/api/analysis/alerts")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        log.warning(f"Vision service unavailable: {e}")
        return JSONResponse({"error": "Vision service unavailable", "alerts": []}, status_code=503)

@app.get("/api/vision/heatmap")
async def vision_heatmap_proxy():
    """Proxy heatmap data from camera-vision service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE}/api/analysis/heatmap")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({}, status_code=503)


@app.get("/api/vision/incidents")
async def vision_incidents_proxy():
    """Proxy incident data from camera-vision service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE}/api/analysis/incidents")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"incidents": {}, "descriptions": {}}, status_code=503)


@app.get("/api/vision/history/{sensor_id}")
async def vision_history_proxy(sensor_id: str, hours: int = 24):
    """Proxy historical traffic data from camera-vision service."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{VISION_SERVICE}/api/analysis/history/{sensor_id}?hours={hours}")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


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


@app.get("/cctv")
async def serve_cctv():
    """CCTV Control Centre — sensor grid view."""
    cctv = STATIC_DIR / "cctv.html"
    if cctv.exists():
        return FileResponse(str(cctv))
    return {"error": "CCTV page not found"}


# ─── NAR Studio + Presenter views ────────────────────────────────────────────

@app.get("/api/studio")
async def api_studio():
    """Aggregated kiosk payload for the studio screen — trunk, towns, top items."""
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse(nar_studio.build_studio(data))


@app.get("/api/presenter")
async def api_presenter():
    """Tagged + sorted list payload for the presenter screen."""
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse(nar_studio.build_presenter(data))


@app.get("/api/road/{code}")
async def api_road(code: str):
    """Drill-down view for one road (e.g. /api/road/A77)."""
    data = cache_get("all_traffic") or await fetch_all_data()
    return JSONResponse(nar_studio.build_road(data, code))


# ─── Bus + train layers (proxy to train-tracker on big-server) ───────────────

TRAIN_TRACKER_URL = "http://127.0.0.1:3974"
AYRSHIRE_BUS_BBOX = {  # tighter than the AYRSHIRE_BBOX; trains tracker covers central belt
    "south": 55.10, "north": 55.95,
    "west": -5.05,  "east":  -4.10,
}


@app.get("/api/buses")
async def api_buses():
    """Live bus positions filtered to Ayrshire (proxy to train-tracker /api/buses)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{TRAIN_TRACKER_URL}/api/buses")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning(f"bus proxy failed: {e}")
        return JSONResponse({"buses": [], "error": str(e), "count": 0}, status_code=200)

    buses = []
    seen = set()
    bbox = AYRSHIRE_BUS_BBOX
    for b in data if isinstance(data, list) else []:
        coords = b.get("coordinates") or [None, None]
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        if lat is None or lon is None:
            continue
        if not (bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lon <= bbox["east"]):
            continue
        bid = b.get("id")
        if bid in seen:
            continue
        seen.add(bid)
        svc = b.get("service") or {}
        veh = b.get("vehicle") or {}
        buses.append({
            "id": bid,
            "lat": lat, "lon": lon,
            "heading": _try_float(b.get("heading")),
            "destination": b.get("destination"),
            "line": svc.get("line_name"),
            "vehicle": veh.get("name"),
            "colour": veh.get("colour") or "#ffcb43",
            "datetime": b.get("datetime"),
        })

    return JSONResponse({"buses": buses, "count": len(buses)})


def _try_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# Static Ayrshire railway stations (CRS code, name, lat, lon)
AYRSHIRE_STATIONS = [
    ("AYR", "Ayr",                    55.4583, -4.6367),
    ("PTW", "Prestwick Town",         55.4955, -4.6147),
    ("PRA", "Prestwick Int. Airport", 55.5097, -4.6094),
    ("TRN", "Troon",                  55.5417, -4.6647),
    ("BSS", "Barassie",               55.5583, -4.6517),
    ("IRV", "Irvine",                 55.6105, -4.6680),
    ("KWN", "Kilwinning",             55.6533, -4.7041),
    ("STV", "Stevenston",             55.6388, -4.7589),
    ("SCO", "Saltcoats",              55.6342, -4.7866),
    ("ARD", "Ardrossan South Beach",  55.6395, -4.8132),
    ("ADS", "Ardrossan Harbour",      55.6406, -4.8186),
    ("WKB", "West Kilbride",          55.6920, -4.8580),
    ("LAR", "Largs",                  55.7948, -4.8716),
    ("FAI", "Fairlie",                55.7592, -4.8608),
    ("DAL", "Dalry",                  55.7088, -4.7185),
    ("KBE", "Kilbirnie / Glengarnock", 55.7383, -4.6700),
    ("MYB", "Maybole",                55.3520, -4.6822),
    ("GIR", "Girvan",                 55.2444, -4.8633),
    ("KMK", "Kilmarnock",             55.6117, -4.4956),
    ("STT", "Stewarton",              55.6837, -4.5158),
]


@app.get("/api/trains/stations")
async def api_train_stations():
    """Static Ayrshire stations + a 'health' summary of next departure delays per station."""
    out = []
    for crs, name, lat, lon in AYRSHIRE_STATIONS:
        out.append({"crs": crs, "name": name, "lat": lat, "lon": lon})
    return JSONResponse({"stations": out, "count": len(out)})


# ─── Weather + warnings + events + calendar (full-tilt extras) ──────────────

AYRWEATHER_URL = "http://127.0.0.1:3875"
PAVILION_EVENT_URL = "https://broadcast.studio.wispayr.online/api/pavilion-festival/event"


@app.get("/api/weather")
async def api_weather(loc: str = "ayr"):
    """Current conditions for a location (default Ayr) — proxy to ayrweather."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{AYRWEATHER_URL}/api/weather/{loc.lower()}")
            r.raise_for_status()
            d = r.json()
    except Exception as e:
        return JSONResponse({"error": str(e), "location": loc}, status_code=200)

    cur = (d.get("forecast") or {}).get("current") or {}
    return JSONResponse({
        "location": (d.get("location") or {}).get("name") or loc.title(),
        "temp_c": cur.get("temperature_2m"),
        "feels_c": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wmo_code": cur.get("weather_code"),
        "wind_mph": cur.get("wind_speed_10m"),
        "gust_mph": cur.get("wind_gusts_10m"),
        "wind_dir": cur.get("wind_direction_10m"),
        "precip_mm": cur.get("precipitation"),
        "cloud_pct": cur.get("cloud_cover"),
        "visibility_m": cur.get("visibility"),
        "pressure_hpa": cur.get("surface_pressure"),
        "measured_at": cur.get("time"),
    })


@app.get("/api/warnings")
async def api_warnings():
    """Met Office warnings + Ayrshire flood warnings — proxied + merged."""
    out = {"met_office": [], "floods": [], "fetched_at": None}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{AYRWEATHER_URL}/api/warnings")
            r.raise_for_status()
            d = r.json()
            out["met_office"] = d.get("warnings", []) or []
            out["fetched_at"] = d.get("fetched_at")
    except Exception as e:
        log.warning(f"warnings proxy: {e}")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{AYRWEATHER_URL}/api/flood/warnings/ayrshire")
            r.raise_for_status()
            d = r.json()
            out["floods"] = d.get("warnings", []) or d.get("items", []) or []
    except Exception as e:
        log.warning(f"flood warnings proxy: {e}")
    out["count"] = len(out["met_office"]) + len(out["floods"])
    return JSONResponse(out)


@app.get("/api/events")
async def api_events():
    """Public events that affect Ayrshire travel — Pavilion Festival etc."""
    items = []
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(PAVILION_EVENT_URL)
            r.raise_for_status()
            ev = r.json()
            if ev and (ev.get("status") in ("upcoming", "live", "active", "in_progress")):
                loc = ev.get("location") or {}
                centre = loc.get("center") or [None, None]
                if len(centre) >= 2 and centre[0] is not None:
                    items.append({
                        "id": ev.get("id") or "pavilion-festival",
                        "name": ev.get("name") or "Pavilion Festival",
                        "type": ev.get("type") or "music_festival",
                        "lat": centre[0], "lon": centre[1],
                        "address": loc.get("address"),
                        "starts": (ev.get("dates") or {}).get("start"),
                        "ends":   (ev.get("dates") or {}).get("end"),
                        "status": ev.get("status"),
                        "bounds": loc.get("bounds"),
                    })
    except Exception as e:
        log.warning(f"events proxy: {e}")
    return JSONResponse({"events": items, "count": len(items)})


@app.get("/api/closures-calendar")
async def api_closures_calendar():
    """7-day calendar of roadworks starts/ends parsed from current data feed."""
    from datetime import date, timedelta, datetime as _dt
    import re as _re

    data = cache_get("all_traffic") or await fetch_all_data()
    today = _dt.now().date()
    days = [today + timedelta(days=i) for i in range(7)]

    # Each item bucketed into days where it starts, ends, or is active
    by_day: dict[str, dict[str, list]] = {d.isoformat(): {"starts": [], "ends": [], "active": []} for d in days}

    # Date pattern: ISO8601 inside descriptions ("From: 2026-05-15T08:00:00")
    iso_re = _re.compile(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}")

    for item in (data.get("roadworks") or []) + (data.get("tros") or []):
        desc = item.get("description") or ""
        title = item.get("title") or ""
        starts_at = ends_at = None
        # Parse "From: ... | Until: ..." pattern that DATEX uses
        m_from = _re.search(r"From:\s*(\d{4}-\d{2}-\d{2})", desc)
        m_to   = _re.search(r"Until:\s*(\d{4}-\d{2}-\d{2})", desc)
        if m_from:
            try: starts_at = _dt.strptime(m_from.group(1), "%Y-%m-%d").date()
            except ValueError: pass
        if m_to:
            try: ends_at = _dt.strptime(m_to.group(1), "%Y-%m-%d").date()
            except ValueError: pass
        if not starts_at and not ends_at:
            continue

        slim = {
            "id": item.get("id"),
            "title": title,
            "description": desc[:200],
            "lat": item.get("lat"), "lon": item.get("lon"),
            "severity": item.get("severity"),
            "type": item.get("type"),
            "starts": starts_at.isoformat() if starts_at else None,
            "ends":   ends_at.isoformat() if ends_at else None,
        }

        for d in days:
            key = d.isoformat()
            if starts_at and d == starts_at:
                by_day[key]["starts"].append(slim)
            elif ends_at and d == ends_at:
                by_day[key]["ends"].append(slim)
            elif starts_at and ends_at and starts_at < d < ends_at:
                by_day[key]["active"].append(slim)

    return JSONResponse({
        "days": [
            {
                "date": d.isoformat(),
                "weekday": d.strftime("%a"),
                "starts": by_day[d.isoformat()]["starts"][:8],
                "ends":   by_day[d.isoformat()]["ends"][:8],
                "active_count": len(by_day[d.isoformat()]["active"]),
                "starts_count": len(by_day[d.isoformat()]["starts"]),
                "ends_count":   len(by_day[d.isoformat()]["ends"]),
            }
            for d in days
        ],
    })


@app.get("/api/trains/board/{crs}")
async def api_train_board(crs: str):
    """Departure board for a station (proxy to train-tracker /api/all from that station)."""
    crs = crs.upper()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{TRAIN_TRACKER_URL}/api/all", params={"crs": crs})
            r.raise_for_status()
            return JSONResponse(r.json())
    except Exception as e:
        log.warning(f"train board proxy failed for {crs}: {e}")
        # Fallback — try without the param (train-tracker serves Ayr-default)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"{TRAIN_TRACKER_URL}/api/all")
                r.raise_for_status()
                return JSONResponse(r.json())
        except Exception as e2:
            return JSONResponse({"error": str(e2), "northbound": {"departures": []}, "southbound": {"departures": []}}, status_code=200)


_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/studio")
async def serve_studio():
    """Now Ayrshire Radio — studio kiosk view."""
    page = STATIC_DIR / "studio.html"
    if page.exists():
        return FileResponse(str(page), headers=_NO_CACHE)
    return {"error": "studio page not found"}


@app.get("/presenter")
async def serve_presenter():
    """Now Ayrshire Radio — presenter desk view."""
    page = STATIC_DIR / "presenter.html"
    if page.exists():
        return FileResponse(str(page), headers=_NO_CACHE)
    return {"error": "presenter page not found"}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# -- Siphon Integration -------------------------------------------------------

async def siphon_get(path: str) -> dict | None:
    """Fetch from Siphon cache service (PRIMARY data source). Returns None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{SIPHON_URL}{path}")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.warning(f"Siphon fetch failed for {path}: {e}")
        return None


@app.get("/api/siphon-incidents")
async def siphon_incidents():
    """Transport Scotland incidents and roadworks via Siphon (supplementary/fallback)."""
    incidents = await siphon_get("/api/transport/incidents")
    roadworks = await siphon_get("/api/transport/roadworks")
    if incidents is None and roadworks is None:
        raise HTTPException(status_code=502, detail="Siphon transport data unavailable")
    return JSONResponse({
        "incidents": incidents if incidents is not None else [],
        "roadworks": roadworks if roadworks is not None else [],
        "source": "siphon",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=PORT, reload=False)
