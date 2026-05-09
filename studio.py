"""
NAR Studio + Presenter views — derived data over the existing /api/traffic payload.

Public surface:
  build_studio(data)     → dict  shape used by /studio kiosk
  build_presenter(data)  → dict  shape used by /presenter list
"""

from __future__ import annotations

import json as _json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─── Ayrshire towns (NAR patch) ───────────────────────────────────────────────
# (name, lat, lon, region) — region used for sub-grouping in UI
TOWNS: list[tuple[str, float, float, str]] = [
    # South Ayrshire
    ("Ayr",          55.4586, -4.6292, "South"),
    ("Prestwick",    55.4953, -4.6147, "South"),
    ("Troon",        55.5424, -4.6655, "South"),
    ("Maybole",      55.3520, -4.6822, "South"),
    ("Girvan",       55.2444, -4.8633, "South"),
    ("Ballantrae",   55.1058, -5.0042, "South"),
    ("Mauchline",    55.5179, -4.3884, "South"),
    ("Dundonald",    55.5683, -4.5806, "South"),

    # East Ayrshire
    ("Kilmarnock",   55.6117, -4.4956, "East"),
    ("Cumnock",      55.4525, -4.2753, "East"),
    ("Galston",      55.6014, -4.3877, "East"),
    ("Newmilns",     55.6017, -4.3253, "East"),
    ("Stewarton",    55.6837, -4.5158, "East"),
    ("Hurlford",     55.6022, -4.4644, "East"),
    ("Auchinleck",   55.4719, -4.2972, "East"),

    # North Ayrshire
    ("Irvine",       55.6105, -4.6675, "North"),
    ("Kilwinning",   55.6533, -4.7028, "North"),
    ("Saltcoats",    55.6334, -4.7872, "North"),
    ("Ardrossan",    55.6431, -4.8127, "North"),
    ("Stevenston",   55.6388, -4.7615, "North"),
    ("Largs",        55.7950, -4.8716, "North"),
    ("West Kilbride",55.6925, -4.8589, "North"),
    ("Beith",        55.7488, -4.6342, "North"),
    ("Dalry",        55.7088, -4.7185, "North"),
    ("Kilbirnie",    55.7522, -4.6817, "North"),
]

# How close (km) an item must be to a town centroid to be tagged to that town.
# 4 km catches built-up areas without bleeding two adjacent towns into each other.
TOWN_RADIUS_KM = 4.0

# Trunk road severity weighting for studio "Most disruptive" sort.
ROAD_WEIGHT = {
    "M77": 5, "A77": 5, "A78": 4, "A71": 3, "A76": 3, "A70": 3,
    "A736": 2, "A737": 2, "A719": 2,
}

SEVERITY_WEIGHT = {"red": 100, "amber": 30, "green": 5, "info": 1}

# Pre-baked OSM road geometry (run scripts/bake_roads.py to refresh)
_ROAD_GEO_DIR = Path(__file__).parent / "data" / "roads"
_road_geo_cache: dict[str, list] = {}


def _load_baked_road_geometry(code: str) -> list:
    """Return cached list of OSM polylines for `code` (e.g. 'A77'), or [] if missing."""
    code = (code or "").upper()
    if code in _road_geo_cache:
        return _road_geo_cache[code]
    path = _ROAD_GEO_DIR / f"{code}.json"
    polylines: list = []
    if path.exists():
        try:
            data = _json.loads(path.read_text())
            if isinstance(data, list):
                polylines = data
        except Exception:
            polylines = []
    _road_geo_cache[code] = polylines
    return polylines


# Recognised trunk/A-roads in Ayrshire — order matters (longest first).
ROAD_REGEX = re.compile(
    r"\b(M77|A77|A78|A736|A737|A719|A70|A71|A76|A726|A735|A739|A8|A8007|A841)\b",
    re.IGNORECASE,
)

# B-road extractor for "minor road" labelling
B_ROAD_REGEX = re.compile(r"\b(B[0-9]{3,4})\b", re.IGNORECASE)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def extract_road(item: dict) -> Optional[str]:
    """Pull the first trunk-road code from title or description."""
    haystack = " ".join(
        str(item.get(k) or "") for k in ("title", "description", "road", "location")
    )
    m = ROAD_REGEX.search(haystack)
    if m:
        return m.group(1).upper()
    m = B_ROAD_REGEX.search(haystack)
    if m:
        return m.group(1).upper()
    return None


def nearest_town(lat: Optional[float], lon: Optional[float]) -> Optional[tuple[str, str, float]]:
    """Return (town, region, distance_km) for the closest town within TOWN_RADIUS_KM, else None."""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    best: Optional[tuple[str, str, float]] = None
    for name, t_lat, t_lon, region in TOWNS:
        d = _haversine_km(lat, lon, t_lat, t_lon)
        if best is None or d < best[2]:
            best = (name, region, d)
    if best is None or best[2] > TOWN_RADIUS_KM:
        return None
    return best


def tag_item(item: dict) -> dict:
    """
    Return a shallow copy of item with derived fields:
      _town       — town name or None
      _region     — North / East / South / None
      _road       — primary road code (trunk or B-road) or None
      _bucket     — "town:Ayr", "road:A77", "ferry", or "regional"
      _score      — sort weight (higher = more important)
      _summary    — one-line factual summary suitable for a presenter
    """
    out = dict(item)

    lat = item.get("lat")
    lon = item.get("lon")
    nt = nearest_town(lat, lon)

    road = extract_road(item)
    out["_road"] = road
    if nt:
        out["_town"], out["_region"], out["_town_dist_km"] = nt[0], nt[1], round(nt[2], 2)
    else:
        out["_town"], out["_region"] = None, None

    if item.get("type") == "ferry":
        out["_bucket"] = "ferry"
    elif nt:
        out["_bucket"] = f"town:{nt[0]}"
    elif road:
        out["_bucket"] = f"road:{road}"
    else:
        out["_bucket"] = "regional"

    sev = (item.get("severity") or "").lower()
    sev_w = SEVERITY_WEIGHT.get(sev, 0)
    road_w = ROAD_WEIGHT.get(road or "", 0)
    type_w = {"incidents": 20, "roadworks": 5, "tro": 4, "ferry": 6, "vms": 1, "travel_time": 0}.get(
        item.get("type") or "", 0
    )
    out["_score"] = sev_w + road_w + type_w

    out["_summary"] = _summary_line(out)
    return out


def _clean_text(s: str, limit: int = 220) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: limit - 1] + "…" if len(s) > limit else s


_NOISE_DESC_RE = re.compile(
    r"^\s*(magnitude\s*:?\s*\d+|delay\s+ratio\s*:?\s*\d+%?|—|-)\s*$",
    re.IGNORECASE,
)


def _is_noise_desc(desc: str) -> bool:
    """Filter out TomTom internal noise like 'Magnitude: 0' / 'Delay ratio: 0%'."""
    if not desc:
        return True
    return bool(_NOISE_DESC_RE.match(desc.strip()))


def _summary_line(item: dict) -> str:
    """One-line factual rendering — presenter-ready facts, not scripted copy.
    Severity badge is rendered separately in the UI, so we don't prefix it here."""
    typ = item.get("type") or "item"
    title = _clean_text(item.get("title") or "", 80)
    desc_raw = item.get("description") or ""
    desc = "" if _is_noise_desc(desc_raw) else _clean_text(desc_raw, 180)

    parts: list[str] = []
    road = item.get("_road")
    town = item.get("_town")
    if road and town:
        parts.append(f"{road} near {town}")
    elif road:
        parts.append(road)
    elif town:
        parts.append(town)

    if title and (not parts or title.lower() not in parts[0].lower()):
        parts.append(title)

    body = " — ".join(parts) if parts else (title or typ.title())

    if desc and desc.lower() not in body.lower():
        body = f"{body}. {desc}"

    return body


# ─── Studio (kiosk) view ──────────────────────────────────────────────────────

TRUNK_ROUTES = ["M77", "A77", "A78", "A71", "A76", "A70", "A736", "A737", "A719"]


def _segment_road(item: dict) -> Optional[str]:
    """Pull the road code from a travel_time site_name (e.g. 'A77 junction 28 to 29' → 'A77')."""
    if item.get("type") != "travel_time":
        return None
    name = (item.get("travel_time_data") or {}).get("site_name") or item.get("title") or ""
    m = ROAD_REGEX.match(name.strip())
    return m.group(1).upper() if m else None


def _segment_delay_min(item: dict) -> float:
    """Delta minutes (current - free_flow). Negative clamped to 0."""
    d = item.get("travel_time_data") or {}
    cur = d.get("travel_time_seconds") or 0
    ff = d.get("free_flow_seconds") or 0
    return max(0.0, (cur - ff) / 60.0)


def _trunk_routes_summary(items: list[dict]) -> list[dict]:
    """Per-route status across the trunk network — combines incidents/roadworks with travel-time delays."""
    by_road: dict[str, list[dict]] = {r: [] for r in TRUNK_ROUTES}
    seg_by_road: dict[str, list[dict]] = {r: [] for r in TRUNK_ROUTES}

    for it in items:
        if it.get("type") in ("incidents", "roadworks", "tro"):
            r = it.get("_road")
            if r in by_road:
                by_road[r].append(it)
        elif it.get("type") == "travel_time":
            r = _segment_road(it)
            if r in seg_by_road:
                seg_by_road[r].append(it)

    out = []
    for r in TRUNK_ROUTES:
        bucket = by_road[r]
        segs = seg_by_road[r]
        red = sum(1 for x in bucket if (x.get("severity") or "").lower() == "red")
        amber = sum(1 for x in bucket if (x.get("severity") or "").lower() == "amber")
        works = sum(1 for x in bucket if x.get("type") == "roadworks")

        # Travel-time aggregates
        total_delay_min = round(sum(_segment_delay_min(s) for s in segs), 1)
        delayed_segs = sum(1 for s in segs if (s.get("travel_time_data") or {}).get("congestion_ratio", 0) >= 0.2)
        worst = max(segs, key=_segment_delay_min, default=None)

        # Status: red beats amber beats green; travel-time also informs
        if red or any((s.get("severity") or "").lower() == "red" for s in segs):
            status = "red"
        elif amber or any((s.get("severity") or "").lower() == "amber" for s in segs) or delayed_segs:
            status = "amber"
        else:
            status = "green"

        top = sorted(bucket, key=lambda x: -x.get("_score", 0))
        out.append({
            "road": r,
            "status": status,
            "incidents": red + amber,
            "roadworks": works,
            "count": len(bucket),
            "delay_min": total_delay_min,
            "delayed_segments": delayed_segs,
            "total_segments": len(segs),
            "worst_segment": (
                {
                    "name": (worst.get("travel_time_data") or {}).get("site_name"),
                    "status": (worst.get("travel_time_data") or {}).get("status"),
                    "delay_min": round(_segment_delay_min(worst), 1),
                    "lat": worst.get("lat"), "lon": worst.get("lon"),
                } if worst else None
            ),
            "top": [
                {"id": t.get("id"), "title": t.get("title"), "summary": t.get("_summary"),
                 "severity": t.get("severity"), "type": t.get("type")}
                for t in top[:3]
            ],
        })
    return out


def _worst_delays(items: list[dict], limit: int = 8) -> list[dict]:
    """Top travel-time segments by absolute delay (minutes), filtered to NAR-relevant trunk roads."""
    relevant = ("M77", "A77", "A78", "A71", "A76", "A70", "A736", "A737", "A719", "M74", "M8")
    segs = []
    for it in items:
        if it.get("type") != "travel_time":
            continue
        d = it.get("travel_time_data") or {}
        if not d:
            continue
        road = _segment_road(it)
        if road not in relevant:
            continue
        delay = _segment_delay_min(it)
        if delay < 0.5 and (d.get("congestion_ratio") or 0) < 0.2:
            continue
        segs.append({
            "id": it.get("id"),
            "road": road,
            "name": d.get("site_name"),
            "status": d.get("status"),
            "current_min": round((d.get("travel_time_seconds") or 0) / 60.0, 1),
            "freeflow_min": round((d.get("free_flow_seconds") or 0) / 60.0, 1),
            "delay_min": round(delay, 1),
            "congestion": round(d.get("congestion_ratio") or 0, 2),
            "severity": it.get("severity"),
            "lat": it.get("lat"), "lon": it.get("lon"),
            "measured_at": d.get("measured_at"),
            "caused_by": it.get("_caused_by") or [],
            "cameras": it.get("_cameras") or [],
        })
    segs.sort(key=lambda s: -s["delay_min"])
    return segs[:limit]


def _heatmap_points(items: list[dict]) -> list[list[float]]:
    """[[lat, lon, intensity 0..1], …] — for Leaflet.heat. Travel-time congestion only."""
    points = []
    for it in items:
        if it.get("type") != "travel_time":
            continue
        lat, lon = it.get("lat"), it.get("lon")
        if lat is None or lon is None:
            continue
        cr = (it.get("travel_time_data") or {}).get("congestion_ratio") or 0
        if cr <= 0:
            continue
        # Clamp 0..1 so blob radius is sensible; >1 = heavy delay anyway
        intensity = min(1.0, max(0.05, cr))
        points.append([float(lat), float(lon), round(intensity, 3)])
    return points


def _camera_index(items: list[dict]) -> list[dict]:
    """Flat list of cameras (id, name, url, lat, lon) for distance lookups."""
    cams: list[dict] = []
    for it in items:
        if it.get("type") != "camera":
            continue
        lat, lon = it.get("lat"), it.get("lon")
        if lat is None or lon is None:
            continue
        cams.append({
            "id": it.get("id"),
            "name": it.get("title") or "CCTV",
            "url": it.get("camera_url") or it.get("url"),
            "lat": float(lat), "lon": float(lon),
        })
    return cams


def _nearest_cameras(lat: Optional[float], lon: Optional[float],
                     cameras: list[dict], max_km: float = 2.0, k: int = 2) -> list[dict]:
    if lat is None or lon is None:
        return []
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return []
    scored: list[tuple[float, dict]] = []
    for c in cameras:
        d = _haversine_km(lat, lon, c["lat"], c["lon"])
        if d <= max_km:
            scored.append((d, c))
    scored.sort(key=lambda x: x[0])
    return [
        {"id": c["id"], "name": c["name"], "url": c["url"],
         "lat": c["lat"], "lon": c["lon"], "distance_km": round(d, 2)}
        for d, c in scored[:k]
    ]


def infer_road_codes(items: list[dict]) -> None:
    """For items lacking a road code (typically TomTom Jam/Road-Closed events),
    snap them to the nearest trunk-road travel-time segment within 2 km.
    Sets `_road` and `_road_inferred = True` in place."""
    INFER_KM = 2.0
    seg_pts: list[tuple[str, float, float]] = []
    for s in items:
        if s.get("type") != "travel_time":
            continue
        sr = _segment_road(s)
        if sr not in TRUNK_ROUTES:
            continue
        slat, slon = s.get("lat"), s.get("lon")
        if slat is None or slon is None:
            continue
        seg_pts.append((sr, float(slat), float(slon)))

    if not seg_pts:
        return

    for it in items:
        if it.get("_road"):
            continue
        if it.get("type") not in ("incidents", "roadworks", "tro", "vms"):
            continue
        ilat, ilon = it.get("lat"), it.get("lon")
        if ilat is None or ilon is None:
            continue
        ilat, ilon = float(ilat), float(ilon)
        best_road = None
        best_dist = INFER_KM
        for sr, slat, slon in seg_pts:
            d = _haversine_km(ilat, ilon, slat, slon)
            if d < best_dist:
                best_dist = d
                best_road = sr
        if best_road:
            it["_road"] = best_road
            it["_road_inferred"] = True
            # Refresh summary with the new road code
            it["_summary"] = _summary_line(it)


def _dedup_items(items: list[dict]) -> list[dict]:
    """Collapse near-identical items (same road + town + title + type) into one,
    keeping the highest-scored instance and noting the duplicate count."""
    by_key: dict[tuple, dict] = {}
    for it in items:
        title = (it.get("title") or "").strip().lower()
        # Strip trailing TomTom magnitude noise from the key so different magnitudes
        # of the same condition collapse together
        title = re.sub(r"\s*[—\-]\s*magnitude.*$", "", title)
        key = (
            it.get("_road") or "",
            it.get("_town") or "",
            title,
            it.get("type") or "",
        )
        if not key[2]:  # no title — don't dedup
            by_key[id(it)] = it
            continue
        existing = by_key.get(key)
        if existing is None:
            it["_dup_count"] = 1
            by_key[key] = it
        else:
            existing["_dup_count"] = existing.get("_dup_count", 1) + 1
            # Keep whichever has the higher score
            if (it.get("_score") or 0) > (existing.get("_score") or 0):
                it["_dup_count"] = existing["_dup_count"]
                by_key[key] = it
    return list(by_key.values())


def correlate(items: list[dict]) -> list[dict]:
    """
    Cross-link items so:
      - travel_time segments with delay get `_caused_by` = nearby incidents/roadworks/VMS
        on the same road code
      - incidents/roadworks/tro get `_impacts` = delayed travel_time segments
        on the same road code
      - both get `_cameras` = 1-2 nearest CCTVs

    Heuristic: same road code AND haversine within threshold.
      - Segment → cause: ≤ 5 km (segments span multiple km, stored as midpoint)
      - Incident → impact: ≤ 1.5 km (incidents are point events, near a segment midpoint
        means the incident is on that segment)
      Confidence band on the segment→cause side:
        high ≤ 1 km, medium ≤ 2.5 km, low ≤ 5 km.
      We never claim "cause"; this surface is "likely causes" / "observed impact".
    """
    SEG_NEAR_KM = 5.0      # segment lat/lon is a midpoint of a multi-km stretch
    INC_NEAR_KM = 1.5

    cameras = _camera_index(items)

    # Index potential causes (incidents/roadworks/tro/vms) by road code
    by_road_cause: dict[str, list[dict]] = {}
    for it in items:
        if it.get("type") not in ("incidents", "roadworks", "tro", "vms"):
            continue
        road = it.get("_road")
        if road:
            by_road_cause.setdefault(road, []).append(it)

    # Index travel_time segments by road code
    by_road_seg: dict[str, list[dict]] = {}
    for it in items:
        if it.get("type") != "travel_time":
            continue
        road = _segment_road(it)
        if road:
            by_road_seg.setdefault(road, []).append(it)

    def _conf(d_km: float) -> str:
        return "high" if d_km <= 1.0 else "medium" if d_km <= 2.5 else "low"

    # Decorate delayed segments with likely causes
    for seg in (it for it in items if it.get("type") == "travel_time"):
        delay = _segment_delay_min(seg)
        slat, slon = seg.get("lat"), seg.get("lon")
        if slat is None or slon is None:
            continue
        seg["_cameras"] = _nearest_cameras(slat, slon, cameras, max_km=4.0, k=2)
        if delay < 0.5:
            continue
        road = _segment_road(seg)
        if not road:
            continue
        causes: list[dict] = []
        for cand in by_road_cause.get(road, []):
            cl, cn = cand.get("lat"), cand.get("lon")
            if cl is None or cn is None:
                continue
            d = _haversine_km(float(slat), float(slon), float(cl), float(cn))
            if d <= SEG_NEAR_KM:
                causes.append({
                    "id": cand.get("id"),
                    "type": cand.get("type"),
                    "summary": cand.get("_summary") or cand.get("title"),
                    "severity": cand.get("severity"),
                    "distance_km": round(d, 2),
                    "confidence": _conf(d),
                    "lat": cand.get("lat"), "lon": cand.get("lon"),
                })
        # Sort: high-confidence first, then nearest
        causes.sort(key=lambda x: (x["confidence"] != "high", x["distance_km"]))
        seg["_caused_by"] = causes[:5]

    # Decorate incidents/roadworks/tro with observed impact
    for inc in (it for it in items if it.get("type") in ("incidents", "roadworks", "tro")):
        ilat, ilon = inc.get("lat"), inc.get("lon")
        if ilat is None or ilon is None:
            continue
        inc["_cameras"] = _nearest_cameras(ilat, ilon, cameras, max_km=3.0, k=2)
        road = inc.get("_road")
        if not road:
            continue
        impacts: list[dict] = []
        for seg in by_road_seg.get(road, []):
            slat, slon = seg.get("lat"), seg.get("lon")
            if slat is None or slon is None:
                continue
            d = _haversine_km(float(ilat), float(ilon), float(slat), float(slon))
            if d > SEG_NEAR_KM:
                continue
            delay = _segment_delay_min(seg)
            if delay < 0.5:
                continue
            tt = seg.get("travel_time_data") or {}
            impacts.append({
                "id": seg.get("id"),
                "name": tt.get("site_name"),
                "delay_min": round(delay, 1),
                "status": tt.get("status"),
                "distance_km": round(d, 2),
                "confidence": _conf(d),
                "lat": seg.get("lat"), "lon": seg.get("lon"),
            })
        impacts.sort(key=lambda x: (-x["delay_min"], x["distance_km"]))
        inc["_impacts"] = impacts[:5]

    return items


def _cameras_relevant_to(top: list[dict], worst: list[dict],
                         all_cameras: list[dict], limit: int = 10) -> list[dict]:
    """Cameras within range of current disruption, deduped, ordered by relevance."""
    seen: set = set()
    out: list[dict] = []

    def _add(cams: list[dict], reason: str):
        for c in cams or []:
            cid = c.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            out.append({**c, "reason": reason})

    # Cameras pinned to the highest-scoring disruption come first
    for t in top:
        _add(t.get("cameras") or [], (t.get("road") or t.get("town") or "incident"))
        if len(out) >= limit:
            break
    # Then worst-delay cameras (segment context)
    for w in worst:
        _add(w.get("cameras") or [], w.get("road") or "delay")
        if len(out) >= limit:
            break

    return out[:limit]


def _vms_messages(items: list[dict], limit: int = 30) -> list[dict]:
    """Active operator messages (variable-message signs)."""
    out = []
    for it in items:
        if it.get("type") != "vms":
            continue
        d = it.get("vms_data") or {}
        msg = (d.get("message") or it.get("description") or "").strip()
        if not msg or not d.get("working", True):
            continue
        out.append({
            "id": it.get("id"),
            "road": d.get("location_name") or it.get("title"),
            "message": msg,
            "time_set": d.get("time_set") or it.get("pub_date"),
            "lat": it.get("lat"), "lon": it.get("lon"),
        })
    # Most recent first
    out.sort(key=lambda x: x.get("time_set") or "", reverse=True)
    return out[:limit]


def _towns_summary(items: list[dict]) -> list[dict]:
    """Per-town status grid."""
    by_town: dict[str, list[dict]] = {name: [] for name, _, _, _ in TOWNS}
    for it in items:
        t = it.get("_town")
        if t and it.get("type") in ("incidents", "roadworks", "tro"):
            by_town.setdefault(t, []).append(it)

    region_for = {name: region for name, _, _, region in TOWNS}
    out = []
    for name, _, _, _ in TOWNS:
        bucket = by_town.get(name, [])
        red = sum(1 for x in bucket if (x.get("severity") or "").lower() == "red")
        amber = sum(1 for x in bucket if (x.get("severity") or "").lower() == "amber")
        if red:
            status = "red"
        elif amber:
            status = "amber"
        elif bucket:
            status = "info"
        else:
            status = "clear"
        top = sorted(bucket, key=lambda x: -x.get("_score", 0))[:2]
        out.append({
            "town": name,
            "region": region_for.get(name, ""),
            "status": status,
            "count": len(bucket),
            "incidents": red + amber,
            "top": [
                {"id": t.get("id"), "summary": t.get("_summary"), "severity": t.get("severity"),
                 "type": t.get("type"), "lat": t.get("lat"), "lon": t.get("lon")}
                for t in top
            ],
        })
    return out


def build_studio(data: dict) -> dict:
    """Studio kiosk payload — passive, glanceable from across the room."""
    raw_all = data.get("all") or []
    tagged = [tag_item(i) for i in raw_all]
    infer_road_codes(tagged)
    correlate(tagged)
    cameras = _camera_index(tagged)

    # Top items overall (red/amber on weighted roads first), deduped, capped to 12
    sortable = [t for t in tagged if t.get("type") in ("incidents", "roadworks", "tro", "ferry")]
    sortable.sort(key=lambda x: -x.get("_score", 0))
    sortable = _dedup_items(sortable)
    sortable.sort(key=lambda x: -x.get("_score", 0))
    top = [
        {
            "id": t.get("id"),
            "type": t.get("type"),
            "severity": t.get("severity"),
            "town": t.get("_town"),
            "region": t.get("_region"),
            "road": t.get("_road"),
            "road_inferred": bool(t.get("_road_inferred")),
            "summary": t.get("_summary"),
            "lat": t.get("lat"),
            "lon": t.get("lon"),
            "title": t.get("title"),
            "description": t.get("description"),
            "impacts": t.get("_impacts") or [],
            "cameras": t.get("_cameras") or [],
            "dup_count": t.get("_dup_count", 1),
        }
        for t in sortable[:12]
    ]

    counts = {
        "incidents": sum(1 for t in tagged if t.get("type") == "incidents"),
        "roadworks": sum(1 for t in tagged if t.get("type") == "roadworks"),
        "tro": sum(1 for t in tagged if t.get("type") == "tro"),
        "ferry": sum(1 for t in tagged if t.get("type") == "ferry"),
        "vms": sum(1 for t in tagged if t.get("type") == "vms"),
    }
    red_total = sum(1 for t in tagged if (t.get("severity") or "").lower() == "red"
                    and t.get("type") in ("incidents", "roadworks", "tro"))
    amber_total = sum(1 for t in tagged if (t.get("severity") or "").lower() == "amber"
                      and t.get("type") in ("incidents", "roadworks", "tro"))

    worst = _worst_delays(tagged)
    cameras_relevant = _cameras_relevant_to(top, worst, cameras, limit=10)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_updated_at": (data.get("meta") or {}).get("updated_at"),
        "headline": {
            "red": red_total,
            "amber": amber_total,
            "incidents": counts["incidents"],
            "roadworks": counts["roadworks"],
            "tro": counts["tro"],
            "ferry": counts["ferry"],
        },
        "trunk": _trunk_routes_summary(tagged),
        "towns": _towns_summary(tagged),
        "top": top,
        "worst_delays": worst,
        "heatmap": _heatmap_points(tagged),
        "vms": _vms_messages(tagged),
        "cameras_all": cameras,
        "cameras_relevant": cameras_relevant,
    }


# ─── Presenter (interactive) view ─────────────────────────────────────────────

def build_road(data: dict, code: str) -> dict:
    """Drill-down payload for a single road code (e.g. 'A77'). Bundles every
    incident, roadworks, TRO, VMS, travel-time segment and nearby camera
    that's tied to this road."""
    code = (code or "").upper()
    raw_all = data.get("all") or []
    tagged = [tag_item(i) for i in raw_all]
    infer_road_codes(tagged)
    correlate(tagged)

    # Collect everything matching this road
    incidents: list[dict] = []
    roadworks: list[dict] = []
    tros: list[dict] = []
    vms: list[dict] = []
    segments: list[dict] = []

    for it in tagged:
        typ = it.get("type")
        if typ == "travel_time":
            if _segment_road(it) == code:
                segments.append(it)
            continue
        if it.get("_road") != code:
            continue
        if typ == "incidents":
            incidents.append(it)
        elif typ == "roadworks":
            roadworks.append(it)
        elif typ == "tro":
            tros.append(it)
        elif typ == "vms":
            vms.append(it)

    # Sort segments by longitude (rough proxy for direction along road)
    segments.sort(key=lambda s: ((s.get("lat") or 0), (s.get("lon") or 0)))

    # Bounding box of everything on this road (used to fit the map). Derived
    # from baked OSM geometry when available so it covers the whole road,
    # otherwise from current items.
    baked_for_bbox = _load_baked_road_geometry(code)
    bbox = None
    if baked_for_bbox:
        all_pts = [pt for line in baked_for_bbox for pt in line]
        if all_pts:
            lats = [p[0] for p in all_pts]
            lons = [p[1] for p in all_pts]
            bbox = {
                "south": min(lats), "north": max(lats),
                "west":  min(lons), "east":  max(lons),
            }
    if bbox is None:
        coords = [
            (it.get("lat"), it.get("lon"))
            for it in incidents + roadworks + tros + vms + segments
            if it.get("lat") is not None and it.get("lon") is not None
        ]
        if coords:
            lats = [c[0] for c in coords]
            lons = [c[1] for c in coords]
            bbox = {
                "south": min(lats), "north": max(lats),
                "west":  min(lons), "east":  max(lons),
            }

    # Cameras within range of the bbox
    all_cams = _camera_index(tagged)
    cameras: list[dict] = []
    if bbox:
        cy = (bbox["south"] + bbox["north"]) / 2
        cx = (bbox["west"]  + bbox["east"])  / 2
        # Scale radius by bbox size, min 5 km
        diag_km = _haversine_km(bbox["south"], bbox["west"], bbox["north"], bbox["east"]) or 5
        radius = max(5.0, diag_km * 0.6)
        scored = []
        seen = set()
        for c in all_cams:
            d = _haversine_km(cy, cx, c["lat"], c["lon"])
            if d <= radius and c["id"] not in seen:
                seen.add(c["id"])
                scored.append((d, c))
        scored.sort(key=lambda x: x[0])
        cameras = [{**c, "distance_km": round(d, 2)} for d, c in scored[:12]]

    # Aggregate stats
    total_delay = round(sum(_segment_delay_min(s) for s in segments), 1)
    delayed_segs = sum(1 for s in segments if (s.get("travel_time_data") or {}).get("congestion_ratio", 0) >= 0.2)
    red = sum(1 for x in incidents + roadworks + tros if (x.get("severity") or "").lower() == "red")
    amber = sum(1 for x in incidents + roadworks + tros if (x.get("severity") or "").lower() == "amber")

    if red or any((s.get("severity") or "").lower() == "red" for s in segments):
        status = "red"
    elif amber or delayed_segs:
        status = "amber"
    else:
        status = "green"

    def _seg_view(s: dict) -> dict:
        d = s.get("travel_time_data") or {}
        return {
            "id": s.get("id"),
            "name": d.get("site_name"),
            "status": d.get("status"),
            "current_min": round((d.get("travel_time_seconds") or 0) / 60.0, 1),
            "freeflow_min": round((d.get("free_flow_seconds") or 0) / 60.0, 1),
            "delay_min": round(_segment_delay_min(s), 1),
            "congestion": round(d.get("congestion_ratio") or 0, 2),
            "lat": s.get("lat"), "lon": s.get("lon"),
            "caused_by": s.get("_caused_by") or [],
        }

    def _item_view(it: dict) -> dict:
        return {
            "id": it.get("id"),
            "type": it.get("type"),
            "severity": it.get("severity"),
            "title": it.get("title"),
            "description": it.get("description"),
            "summary": it.get("_summary"),
            "town": it.get("_town"),
            "lat": it.get("lat"), "lon": it.get("lon"),
            "pub_date": it.get("pub_date"),
            "source": it.get("source"),
            "url": it.get("url"),
        }

    # ─── Geometry ─────────────────────────────────────────────────────────
    # `geometry` is the full pre-baked OSM road shape (the whole route, drawn
    # subtly underneath); `hot_geometry` is the live TomTom-incident polylines
    # (drawn brightly on top to mark current trouble zones).
    geometry: list[list[list[float]]] = _load_baked_road_geometry(code)

    hot_geometry: list[list[list[float]]] = []
    item_ids_for_geom = {it.get("id") for it in (incidents + roadworks + tros)}
    for it in tagged:
        if it.get("id") not in item_ids_for_geom:
            continue
        geo = it.get("tomtom_geometry")
        if isinstance(geo, list) and len(geo) >= 2:
            line: list[list[float]] = []
            for p in geo:
                try:
                    line.append([float(p[0]), float(p[1])])
                except (TypeError, ValueError, IndexError):
                    continue
            if len(line) >= 2:
                hot_geometry.append(line)

    # If we have neither pre-baked geometry nor TomTom hot zones, synthesise
    # a path from segment midpoints as a last-resort fallback.
    if not geometry and not hot_geometry and segments:
        seg_points = []
        for s in segments:
            slat, slon = s.get("lat"), s.get("lon")
            if slat is not None and slon is not None:
                seg_points.append([float(slat), float(slon)])
        seg_points.sort(key=lambda p: (p[1], p[0]))
        if len(seg_points) >= 2:
            geometry.append(seg_points)

    return {
        "code": code,
        "status": status,
        "summary": {
            "total_delay_min": total_delay,
            "delayed_segments": delayed_segs,
            "total_segments": len(segments),
            "incidents": len(incidents),
            "roadworks": len(roadworks),
            "tros": len(tros),
            "vms": len(vms),
            "red": red, "amber": amber,
        },
        "bbox": bbox,
        "geometry": geometry,
        "hot_geometry": hot_geometry,
        "segments": [_seg_view(s) for s in segments],
        "incidents": [_item_view(x) for x in incidents],
        "roadworks": [_item_view(x) for x in roadworks],
        "tros":      [_item_view(x) for x in tros],
        "vms":       [_item_view(x) for x in vms],
        "cameras":   cameras,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_presenter(data: dict) -> dict:
    """Presenter payload — full tagged list with grouping helpers."""
    raw_all = data.get("all") or []
    tagged = [tag_item(i) for i in raw_all]
    infer_road_codes(tagged)
    correlate(tagged)
    # Sort by score desc, then severity, then type
    tagged.sort(key=lambda x: (-x.get("_score", 0), x.get("type") or ""))

    items = []
    for t in tagged:
        # For travel_time items, harvest road from site_name if no _road set
        road = t.get("_road")
        if not road:
            road = _segment_road(t)
        items.append({
            "id": t.get("id"),
            "type": t.get("type"),
            "severity": t.get("severity"),
            "title": t.get("title"),
            "description": t.get("description"),
            "summary": t.get("_summary"),
            "town": t.get("_town"),
            "region": t.get("_region"),
            "road": road,
            "bucket": t.get("_bucket"),
            "lat": t.get("lat"),
            "lon": t.get("lon"),
            "url": t.get("url"),
            "source": t.get("source"),
            "pub_date": t.get("pub_date"),
            "score": t.get("_score"),
            "travel_time_data": t.get("travel_time_data"),
            "vms_data": t.get("vms_data"),
            "caused_by": t.get("_caused_by") or [],
            "impacts": t.get("_impacts") or [],
            "cameras": t.get("_cameras") or [],
        })

    # Facets for filter UI
    towns = sorted({i["town"] for i in items if i["town"]})
    roads = sorted({i["road"] for i in items if i["road"]})
    sources = sorted({i["source"] for i in items if i["source"]})
    types = sorted({i["type"] for i in items if i["type"]})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_updated_at": (data.get("meta") or {}).get("updated_at"),
        "items": items,
        "facets": {
            "towns": towns,
            "roads": roads,
            "sources": sources,
            "types": types,
        },
        "counts": {
            "total": len(items),
            "incidents": sum(1 for i in items if i["type"] == "incidents"),
            "roadworks": sum(1 for i in items if i["type"] == "roadworks"),
            "tro": sum(1 for i in items if i["type"] == "tro"),
            "ferry": sum(1 for i in items if i["type"] == "ferry"),
            "vms": sum(1 for i in items if i["type"] == "vms"),
            "red": sum(1 for i in items if (i.get("severity") or "").lower() == "red"
                      and i.get("type") in ("incidents", "roadworks", "tro")),
            "amber": sum(1 for i in items if (i.get("severity") or "").lower() == "amber"
                        and i.get("type") in ("incidents", "roadworks", "tro")),
        },
    }
