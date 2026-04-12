"""
nuro Camera Vision Service — analyses CCTV sensor images
Runs as a nuro service on small-server. Uses Pillow for lightweight local analysis.
Heavy inference (vehicle counting) delegated to nuro inference nodes when available.

Local analysis capabilities (no GPU needed):
- Brightness/exposure → day/night/dusk detection
- Colour histogram → weather estimation (grey=overcast, blue-sky=clear, etc.)
- Frame differencing → activity/motion level between pulls
- Perceptual hash → stuck camera detection (same image = offline/frozen)
- Scene classification → basic road/environment categorisation

Reports results as nuro sensor streams via /api/nuro endpoint.
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image, ImageStat

# ─── Config ──────────────────────────────────────────────────────────────────

PORT = 3878
CAMERA_DIR = Path("/opt/ayrtraffic/data/cameras")
ANALYSIS_INTERVAL = 300       # Run analysis every 5 minutes
AYRTRAFFIC_URL = "http://localhost:3876"
NURO_HUB = "http://10.200.0.8:3960"
BRAVO_VISION_URL = "http://10.201.0.2:8878"  # Bravo M4 Pro - YOLO vehicle detection via WireGuard
DB_DSN = "postgresql://postgres:traffic2026@localhost/ayrtraffic"
NURO_ALERT_URL = f"{NURO_HUB}/api/alerts"
SERVICE_ID = "camera-vision"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [CameraVision] %(message)s")
log = logging.getLogger("camera-vision")

# ─── Analysis State ──────────────────────────────────────────────────────────

_analysis_results = {}      # sensor_id -> latest analysis
_previous_hashes = {}       # sensor_id -> previous perceptual hash
_previous_brightness = {}   # sensor_id -> list of recent brightness values
_analysis_summary = {}      # aggregate stats
_last_analysis_time = 0
_inference_queue = []        # images queued for remote inference
_frozen_counts = {}          # sensor_id -> consecutive frozen frame count
_road_types = {}             # sensor_id -> road type classification (re-cached on restart)
_vehicle_results = {}        # sensor_id -> latest vehicle detection from Bravo
_bravo_available = True      # track if Bravo is reachable
_db_pool = None              # asyncpg connection pool
_scene_descriptions = {}     # sensor_id -> latest LLM description
_incidents = {}              # sensor_id -> latest incidents
_active_alerts = {}          # sensor_id -> active alert types (for dedup)


# ─── Pillow-Based Analysis Functions ─────────────────────────────────────────

def analyse_brightness(img: Image.Image) -> dict:
    """Analyse image brightness/exposure."""
    grey = img.convert("L")
    stat = ImageStat.Stat(grey)
    mean_brightness = stat.mean[0]        # 0-255
    std_brightness = stat.stddev[0]

    # Classify time of day from brightness
    if mean_brightness < 30:
        time_of_day = "night"
    elif mean_brightness < 60:
        time_of_day = "dusk"
    elif mean_brightness < 90:
        time_of_day = "overcast"
    elif mean_brightness < 160:
        time_of_day = "daylight"
    else:
        time_of_day = "bright"

    # Low contrast might indicate fog
    fog_likelihood = max(0, min(100, int(100 - std_brightness * 2)))

    return {
        "mean_brightness": round(mean_brightness, 1),
        "std_brightness": round(std_brightness, 1),
        "time_of_day": time_of_day,
        "fog_likelihood": fog_likelihood,
    }


def analyse_weather(img: Image.Image) -> dict:
    """Estimate weather from colour distribution."""
    # Resize for speed
    thumb = img.resize((64, 64))
    pixels = list(thumb.getdata())

    r_vals = [p[0] for p in pixels]
    g_vals = [p[1] for p in pixels]
    b_vals = [p[2] for p in pixels]

    avg_r = statistics.mean(r_vals)
    avg_g = statistics.mean(g_vals)
    avg_b = statistics.mean(b_vals)

    # Colour temperature / cast
    warmth = avg_r - avg_b  # positive = warm/sunny, negative = cold/blue

    # Grey uniformity (overcast skies are uniform grey)
    rgb_spread = statistics.stdev([avg_r, avg_g, avg_b])

    # Saturation estimate
    saturation = max(avg_r, avg_g, avg_b) - min(avg_r, avg_g, avg_b)

    # Classify
    if rgb_spread < 8 and saturation < 20:
        weather = "overcast"
    elif warmth > 20 and saturation > 40:
        weather = "sunny"
    elif avg_b > avg_r + 15 and avg_b > avg_g:
        weather = "clear_sky"
    elif statistics.mean([avg_r, avg_g, avg_b]) < 50:
        weather = "dark"
    else:
        weather = "mixed"

    # Wet road detection — multi-signal approach
    w, h = img.size

    # Road area = lower 40% of image
    road_crop = img.crop((0, int(h * 0.6), w, h))
    road_grey = road_crop.convert("L")
    road_stat = ImageStat.Stat(road_grey)
    road_rgb_stat = ImageStat.Stat(road_crop)

    # Sky area = upper 30%
    sky_crop = img.crop((0, 0, w, int(h * 0.3)))
    sky_stat = ImageStat.Stat(sky_crop.convert("L"))

    road_bri = road_stat.mean[0]
    road_std = road_stat.stddev[0]
    road_r, road_g, road_b = road_rgb_stat.mean
    sky_bri = sky_stat.mean[0]

    wet_road_score = 0
    wet_signals = []

    # Signal 1: Blue shift in road area (wet roads reflect blue sky)
    # Dry road: neutral grey (R≈G≈B). Wet: blue-shifted (B > avg(R,G))
    blue_shift = road_b - (road_r + road_g) / 2
    if blue_shift > 8:
        wet_signals.append(min(30, int((blue_shift - 8) * 3)))

    # Signal 2: Specular highlights (bright spots from water pooling)
    road_pixels = list(road_grey.getdata())
    bright_pct = sum(1 for p in road_pixels if p > 180) / max(len(road_pixels), 1) * 100
    if bright_pct > 2:
        wet_signals.append(min(25, int(bright_pct * 4)))

    # Signal 3: Road-to-sky brightness ratio
    # Dry road: ~0.4-0.6 of sky brightness. Wet: >0.7 (reflects more light)
    if sky_bri > 50:  # only meaningful when sky is visible
        refl_ratio = road_bri / sky_bri
        if refl_ratio > 0.75:
            wet_signals.append(min(25, int((refl_ratio - 0.75) * 100)))

    # Signal 4: High local contrast in road area (standing water creates bright/dark patches)
    if road_std > 35:
        wet_signals.append(min(20, int((road_std - 35) * 1.5)))

    wet_road_score = min(100, sum(wet_signals))

    return {
        "weather": weather,
        "warmth": round(warmth, 1),
        "saturation": round(saturation, 1),
        "wet_road_score": wet_road_score,
        "wet_signals": {
            "blue_shift": round(blue_shift, 1),
            "bright_pct": round(bright_pct, 1),
            "reflection_ratio": round(road_bri / max(sky_bri, 1), 2),
            "road_contrast": round(road_std, 1),
        },
        "avg_rgb": [round(avg_r), round(avg_g), round(avg_b)],
    }


def perceptual_hash(img: Image.Image, size=8) -> str:
    """Compute a perceptual hash (average hash) for stuck-camera detection."""
    grey = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(grey.getdata())
    avg = statistics.mean(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return hex(int(bits, 2))[2:].zfill(size * size // 4)


def hamming_distance(h1: str, h2: str) -> int:
    """Count bit differences between two hex hashes."""
    b1 = bin(int(h1, 16))[2:]
    b2 = bin(int(h2, 16))[2:]
    max_len = max(len(b1), len(b2))
    b1 = b1.zfill(max_len)
    b2 = b2.zfill(max_len)
    return sum(c1 != c2 for c1, c2 in zip(b1, b2))


def analyse_activity(img: Image.Image, sensor_id: str) -> dict:
    """Detect scene change / activity by comparing to previous image hash."""
    current_hash = perceptual_hash(img)
    prev_hash = _previous_hashes.get(sensor_id)
    _previous_hashes[sensor_id] = current_hash

    if prev_hash is None:
        return {"scene_change": None, "activity_level": "unknown", "hash": current_hash}

    distance = hamming_distance(current_hash, prev_hash)
    max_distance = 64  # 8x8 hash

    # Normalise to percentage
    change_pct = round(distance / max_distance * 100)

    if change_pct < 3:
        # Only flag frozen if seen consecutively (not just within same FTP pull)
        _frozen_counts[sensor_id] = _frozen_counts.get(sensor_id, 0) + 1
        if _frozen_counts[sensor_id] >= 3:  # 3 cycles = 15 min, spans at least 1 FTP pull
            activity = "frozen"
        else:
            activity = "static"  # Not yet confirmed frozen
    elif change_pct < 10:
        activity = "static"   # Very little change (empty road)
    elif change_pct < 25:
        activity = "light"    # Some traffic
    elif change_pct < 45:
        activity = "moderate" # Normal traffic
    else:
        activity = "heavy"    # Lots of movement / congestion

    # Reset frozen counter on scene change
    if change_pct >= 3:
        _frozen_counts[sensor_id] = 0

    return {
        "scene_change_pct": change_pct,
        "activity_level": activity,
        "hash": current_hash,
        "prev_hash": prev_hash,
    }


def analyse_road_area(img: Image.Image) -> dict:
    """Analyse the road area (lower 60% of image) for vehicle density hints."""
    # Road is typically in the lower portion
    road_crop = img.crop((0, int(img.height * 0.4), img.width, img.height))
    grey = road_crop.convert("L")
    stat = ImageStat.Stat(grey)

    # Higher entropy in road area = more vehicles/objects
    # Calculate rough entropy
    hist = grey.histogram()
    total = sum(hist)
    entropy = 0
    for h in hist:
        if h > 0:
            p = h / total
            entropy -= p * math.log2(p)

    # Normalise entropy (max ~8 for uniform)
    density_score = min(100, int(entropy * 12.5))

    # Edge density (more edges = more objects)
    # Simple gradient estimation
    pixels = list(grey.getdata())
    w, h = grey.size
    edge_sum = 0
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            idx = y * w + x
            dx = abs(pixels[idx + 1] - pixels[idx - 1])
            dy = abs(pixels[idx + w] - pixels[idx - w])
            edge_sum += dx + dy
    edge_density = edge_sum / (w * h) if w * h > 0 else 0

    return {
        "road_entropy": round(entropy, 2),
        "density_score": density_score,
        "edge_density": round(edge_density, 1),
    }


def analyse_road_type(img: Image.Image) -> dict:
    """Classify road type from camera image.

    Improved approach:
    - Tighter road pixel mask excluding sky, vegetation, bright structures
    - Better edge detection with proper debouncing (min gap between edges)
    - Central reservation only checked where road is actually detected
    - Lane width estimation from road extent / edge count
    """
    w, h = img.size

    # Work on lower 55% of image (road area, avoid sky contamination)
    road_region_top = int(h * 0.45)
    road_crop = img.crop((0, road_region_top, w, h))
    rw, rh = road_crop.size

    grey = road_crop.convert("L")
    rgb_data = list(road_crop.getdata())
    grey_data = list(grey.getdata())

    # Step 1: Identify road pixels with tighter mask
    # Road = low saturation, medium brightness, NOT green (vegetation), NOT bright blue (sky)
    road_mask = []
    for i, (r, g, b) in enumerate(rgb_data):
        brightness = grey_data[i]
        max_rgb = max(r, g, b)
        min_rgb = min(r, g, b)
        sat = (max_rgb - min_rgb) / max(max_rgb, 1)

        # Exclude vegetation (green dominant)
        green_dominant = g > r + 10 and g > b + 10
        # Exclude sky (bright + blue dominant)
        sky_like = brightness > 150 and b > r and b > g and sat > 0.08
        # Exclude very bright (markings, sky reflection, structures)
        too_bright = brightness > 210

        is_road = sat < 0.22 and 35 < brightness < 210 and not green_dominant and not sky_like and not too_bright
        road_mask.append(is_road)

    # Step 2: Scan multiple rows for road metrics
    scan_lines = [int(rh * p) for p in [0.25, 0.35, 0.45, 0.55, 0.65, 0.75]]
    road_widths = []
    lane_edge_counts = []
    road_coverages = []
    central_gap_detected = []

    for sy in scan_lines:
        if sy >= rh:
            continue

        row_start = sy * rw
        row = road_mask[row_start:row_start + rw]

        # Find contiguous road extent (skip isolated pixels)
        road_pixels = [x for x in range(rw) if row[x]]
        if len(road_pixels) < 8:
            continue

        # Use the densest road region, not just left-right extent
        # Find the longest contiguous run of mostly-road pixels
        left = road_pixels[0]
        right = road_pixels[-1]
        road_width = right - left
        if road_width < 20:
            continue

        # Road coverage within the extent (should be >60% to be real road, not scattered pixels)
        extent_road = sum(1 for x in range(left, right + 1) if row[x])
        coverage = extent_road / max(road_width, 1)
        if coverage < 0.35:
            continue

        road_widths.append(road_width / rw)
        road_coverages.append(coverage)

        # Edge detection with proper debouncing
        row_grey = grey_data[row_start:row_start + rw]
        window = 4
        edge_positions = []
        for x in range(left + window, right - window):
            left_avg = sum(row_grey[x - window:x]) / window
            right_avg = sum(row_grey[x + 1:x + 1 + window]) / window
            diff = abs(right_avg - left_avg)
            if diff > 30:  # higher threshold to avoid noise
                edge_positions.append(x)

        # Debounce: merge edges within 8px of each other
        debounced = []
        for pos in edge_positions:
            if not debounced or pos - debounced[-1] > 8:
                debounced.append(pos)
        lane_edge_counts.append(len(debounced))

        # Central reservation: look for a continuous non-road gap in the middle 40%
        mid_start = left + int(road_width * 0.3)
        mid_end = right - int(road_width * 0.3)
        if mid_end > mid_start + 5:
            mid_section = [row[x] for x in range(mid_start, mid_end)]
            # Find longest continuous gap of non-road pixels
            max_gap = 0
            current_gap = 0
            for is_road_px in mid_section:
                if not is_road_px:
                    current_gap += 1
                    max_gap = max(max_gap, current_gap)
                else:
                    current_gap = 0
            # Central reservation = continuous gap of >5% of road width
            gap_pct = max_gap / max(road_width, 1)
            central_gap_detected.append(gap_pct > 0.05 and max_gap > 5)

    if not road_widths:
        return {
            "road_type": "unknown",
            "lane_estimate": 0,
            "road_width_pct": 0,
            "confidence": 0,
            "has_central_reservation": False,
        }

    avg_road_width = statistics.mean(road_widths)
    avg_edges = statistics.mean(lane_edge_counts) if lane_edge_counts else 0
    avg_coverage = statistics.mean(road_coverages)
    edge_variance = statistics.stdev(lane_edge_counts) if len(lane_edge_counts) > 1 else 0

    # Central reservation: detected in majority of scan lines
    has_central = sum(central_gap_detected) > len(central_gap_detected) * 0.5 if central_gap_detected else False

    # Lane estimation: edges include road boundaries + lane markings
    # For close-up cameras (road fills >90% of frame), width is uninformative
    # so rely more heavily on edge count
    inner_edges = max(0, avg_edges - 2)
    lane_estimate = max(1, round(inner_edges + 1))

    # Cap at reasonable values — most roads have max 6 lanes per direction
    lane_estimate = min(lane_estimate, 8)

    # If few edges, likely single regardless of apparent width
    if avg_edges <= 3:
        lane_estimate = min(lane_estimate, 2)
    elif avg_edges <= 5:
        lane_estimate = min(lane_estimate, 3)

    # Classify
    confidence = 0

    if edge_variance > 3.5 and avg_edges > 5:
        road_type = "complex"
        confidence = min(85, int(50 + edge_variance * 5))
        road_detail = f"junction ({lane_estimate} lanes detected)"
    elif has_central and avg_edges >= 8:
        road_type = "motorway"
        confidence = min(85, int(45 + lane_estimate * 8))
        road_detail = f"{lane_estimate}-lane motorway"
    elif has_central and avg_edges >= 4:
        road_type = "dual_carriageway"
        confidence = min(80, int(40 + lane_estimate * 10))
        road_detail = f"{lane_estimate}-lane dual carriageway"
    elif avg_edges >= 6:
        road_type = "dual_carriageway"
        confidence = min(65, int(30 + lane_estimate * 7))
        road_detail = f"{lane_estimate}-lane road"
    elif avg_edges <= 3 and not has_central:
        road_type = "single_carriageway"
        confidence = min(75, int(45 + (4 - avg_edges) * 8))
        road_detail = f"{lane_estimate}-lane single road"
    elif has_central:
        road_type = "dual_carriageway"
        confidence = 55
        road_detail = f"{lane_estimate}-lane dual carriageway"
    elif avg_edges <= 5:
        road_type = "dual_carriageway"
        confidence = 50
        road_detail = f"{lane_estimate}-lane road"
    else:
        road_type = "dual_carriageway"
        confidence = 45
        road_detail = f"{lane_estimate}-lane road"

    return {
        "road_type": road_type,
        "road_detail": road_detail,
        "lane_estimate": lane_estimate,
        "road_width_pct": round(avg_road_width * 100, 1),
        "road_coverage_pct": round(avg_coverage * 100, 1),
        "edge_density": round(avg_edges, 1),
        "edge_variance": round(edge_variance, 1),
        "has_central_reservation": has_central,
        "central_gap_pct": round(sum(central_gap_detected) / max(len(central_gap_detected), 1) * 100, 1),
        "confidence": confidence,
    }


def analyse_broken_camera(img: Image.Image, image_path: Path) -> dict:
    """Detect broken/placeholder camera images.

    Checks for:
    - Single-colour frames (dead camera, lens cap, black/grey screen)
    - Very low entropy (uniform colour with minor noise)
    - "Awaiting image" or placeholder text indicators (small file + flat colour)
    """
    grey = img.convert("L")
    stat = ImageStat.Stat(grey)
    mean_b = stat.mean[0]
    std_b = stat.stddev[0]

    # Also check colour channels
    rgb_stat = ImageStat.Stat(img)
    rgb_stds = rgb_stat.stddev  # [R_std, G_std, B_std]
    max_channel_std = max(rgb_stds)

    # File size check — placeholder images tend to be very small
    try:
        file_size = os.path.getsize(image_path)
    except Exception:
        file_size = 0

    broken = False
    reason = None

    # Check 1: Nearly uniform colour (std dev < 5 across all channels)
    # This catches solid black, grey, blue placeholder screens
    if max_channel_std < 5:
        broken = True
        if mean_b < 15:
            reason = "black_frame"
        elif mean_b > 240:
            reason = "white_frame"
        else:
            reason = "solid_colour"

    # Check 2: Very low entropy — slightly noisy but still essentially one colour
    # std < 12 with small file = likely placeholder with compression artefacts
    elif max_channel_std < 12 and file_size < 5000:
        broken = True
        reason = "placeholder"

    # Check 3: Extremely small file for a 320x240 JPEG — likely text-only placeholder
    # "Awaiting image" placeholders are typically < 3KB
    elif file_size < 2000 and std_b < 20:
        broken = True
        reason = "awaiting_image"

    # Check 4: Image is nearly all one colour but has a small text region
    # Typical pattern: grey/blue background with white text saying "No image"
    # Detect by checking if >90% of pixels are within narrow range
    if not broken:
        pixels = list(grey.getdata())
        total = len(pixels)
        # Count pixels within ±10 of the mean
        near_mean = sum(1 for p in pixels if abs(p - mean_b) < 10)
        uniformity = near_mean / total if total > 0 else 0
        if uniformity > 0.92 and file_size < 8000:
            broken = True
            reason = "text_placeholder"

    return {
        "broken": broken,
        "reason": reason,
        "pixel_std": round(max_channel_std, 1),
        "file_size": file_size,
    }


def analyse_rain_on_lens(img: Image.Image) -> dict:
    """Detect rain droplets / water on the camera lens.

    Method:
    - Compute Laplacian-like sharpness across image quadrants
    - Rain droplets cause localised blur → high sharpness variance between regions
    - Overall low sharpness + high variance = likely rain on lens
    - Compare top half vs bottom half (rain accumulates at top of lens housing)
    """
    grey = img.convert("L")
    w, h = grey.size

    def region_sharpness(region):
        """Compute sharpness of a region using variance of pixel gradients."""
        pixels = list(region.getdata())
        rw, rh = region.size
        if rw < 4 or rh < 4:
            return 0
        grad_sum = 0
        grad_sq_sum = 0
        count = 0
        for y in range(1, rh - 1):
            for x in range(1, rw - 1):
                idx = y * rw + x
                # Laplacian approximation
                lap = abs(
                    4 * pixels[idx]
                    - pixels[idx - 1] - pixels[idx + 1]
                    - pixels[idx - rw] - pixels[idx + rw]
                )
                grad_sum += lap
                grad_sq_sum += lap * lap
                count += 1
        if count == 0:
            return 0
        mean = grad_sum / count
        variance = grad_sq_sum / count - mean * mean
        return variance

    # Divide into 4x4 grid of regions
    grid_size = 4
    rw = w // grid_size
    rh = h // grid_size
    sharpness_values = []
    top_sharpness = []
    bottom_sharpness = []

    for gy in range(grid_size):
        for gx in range(grid_size):
            region = grey.crop((gx * rw, gy * rh, (gx + 1) * rw, (gy + 1) * rh))
            # Downsample for speed
            region = region.resize((rw // 2, rh // 2), Image.Resampling.NEAREST)
            s = region_sharpness(region)
            sharpness_values.append(s)
            if gy < grid_size // 2:
                top_sharpness.append(s)
            else:
                bottom_sharpness.append(s)

    if not sharpness_values or max(sharpness_values) == 0:
        return {"rain_score": 0, "rain_detected": False, "lens_condition": "unknown"}

    avg_sharpness = statistics.mean(sharpness_values)
    sharpness_std = statistics.stdev(sharpness_values) if len(sharpness_values) > 1 else 0
    avg_top = statistics.mean(top_sharpness) if top_sharpness else 0
    avg_bottom = statistics.mean(bottom_sharpness) if bottom_sharpness else 0

    # Rain indicators — calibrated for 320x240 FTP camera images
    # Typical values: avg_sharpness 3000-10000, CV ratio 0.8-3.0
    cv_ratio = sharpness_std / max(avg_sharpness, 1)

    # 1. High variation between regions (droplets cause uneven localised blur)
    # Normal CV is ~1.5-2.2; rain pushes this higher with patchy blur
    if cv_ratio > 2.8:
        variation_score = min(50, int((cv_ratio - 2.8) * 100))
    else:
        variation_score = 0

    # 2. Top half blurrier than bottom (water runs down from top of lens housing)
    if avg_bottom > 0:
        top_bottom_ratio = avg_top / avg_bottom
    else:
        top_bottom_ratio = 1.0
    # Only score if top is notably blurrier (ratio < 0.6)
    if top_bottom_ratio < 0.6:
        top_degraded = min(30, int((0.6 - top_bottom_ratio) * 75))
    else:
        top_degraded = 0

    # 3. Overall low sharpness (wet lens = general blur)
    # Normal sharp image has sharpness > 4000; blurry/wet < 3000
    if avg_sharpness < 3000:
        blur_score = min(30, int((3000 - avg_sharpness) / 50))
    else:
        blur_score = 0

    # Combined rain score
    rain_score = min(100, variation_score + top_degraded + blur_score)

    # Classify — conservative thresholds to avoid false positives
    if rain_score > 55:
        lens_condition = "rain_heavy"
        rain_detected = True
    elif rain_score > 30:
        lens_condition = "rain_light"
        rain_detected = True
    elif rain_score > 15:
        lens_condition = "mist"
        rain_detected = False
    else:
        lens_condition = "clear"
        rain_detected = False

    return {
        "rain_score": rain_score,
        "rain_detected": rain_detected,
        "lens_condition": lens_condition,
        "avg_sharpness": round(avg_sharpness, 1),
        "sharpness_variation": round(sharpness_std, 1),
        "top_vs_bottom": round(top_bottom_ratio, 2),
    }


async def analyse_vehicles_bravo(camera_images: dict):
    """Send camera images to Bravo for YOLO vehicle detection.
    camera_images: dict of sensor_id -> image_path
    Updates _vehicle_results in-place.
    """
    global _vehicle_results, _bravo_available

    if not camera_images:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        # Check health first
        try:
            resp = await client.get(f"{BRAVO_VISION_URL}/health")
            if resp.status_code != 200:
                _bravo_available = False
                log.warning("Bravo vision not healthy")
                return
            _bravo_available = True
        except Exception:
            _bravo_available = False
            log.warning("Bravo vision unreachable")
            return

        # Process cameras individually
        processed = 0
        for sensor_id, img_path in camera_images.items():
            try:
                # Get road type for incident analysis
                rt = _road_types.get(sensor_id, {}).get("road_type", "")
                with open(img_path, "rb") as f:
                    files = {"file": (f"{sensor_id}.jpg", f, "image/jpeg")}
                    resp = await client.post(
                        f"{BRAVO_VISION_URL}/api/detect",
                        files=files,
                        params={"conf": 0.20, "road_type": rt}
                    )
                if resp.status_code == 200:
                    _vehicle_results[sensor_id] = resp.json()
                    processed += 1
            except Exception as e:
                log.debug(f"Vehicle detection failed for {sensor_id}: {e}")

        log.info(f"Bravo vehicle detection: {processed}/{len(camera_images)} cameras processed")


def analyse_single_camera(sensor_id: str, image_path: Path) -> dict:
    """Run all local analysis on a single camera image."""
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        return {"error": str(e), "sensor_id": sensor_id}

    # Check for broken/placeholder cameras first
    broken = analyse_broken_camera(img, image_path)
    if broken["broken"]:
        return {
            "sensor_id": sensor_id,
            "analysed_at": datetime.now(timezone.utc).isoformat(),
            "broken": broken,
            "brightness": {"mean_brightness": 0, "std_brightness": 0, "time_of_day": "unknown", "fog_likelihood": 0},
            "weather": {"weather": "unknown"},
            "activity": {"activity_level": "broken", "scene_change": None},
            "rain": {"rain_score": 0, "rain_detected": False, "lens_condition": "unknown"},
            "road_type": {"road_type": "unknown", "lane_estimate": 0, "confidence": 0},
            "road": None,
            "image_size": os.path.getsize(image_path),
        }

    brightness = analyse_brightness(img)
    weather = analyse_weather(img)
    activity = analyse_activity(img, sensor_id)

    # Rain on lens detection
    rain = analyse_rain_on_lens(img)

    # Road type classification (run once then cache — road type doesn't change)
    if sensor_id not in _road_types:
        _road_types[sensor_id] = analyse_road_type(img)
    road_type = _road_types[sensor_id]

    # Only run road analysis on a subset (it's CPU-intensive)
    road = {}
    # Run on ~20% of cameras each cycle to spread load
    if hash(sensor_id + str(int(time.time() / ANALYSIS_INTERVAL))) % 5 == 0:
        road = analyse_road_area(img)

    # Track brightness history for trend detection
    if sensor_id not in _previous_brightness:
        _previous_brightness[sensor_id] = []
    _previous_brightness[sensor_id].append(brightness["mean_brightness"])
    # Keep last 12 readings (1 hour at 5-min intervals)
    _previous_brightness[sensor_id] = _previous_brightness[sensor_id][-12:]

    # Attach vehicle detection from Bravo if available
    vehicles = _vehicle_results.get(sensor_id, {})

    return {
        "sensor_id": sensor_id,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
        "broken": {"broken": False, "reason": None},
        "brightness": brightness,
        "weather": weather,
        "activity": activity,
        "rain": rain,
        "road_type": road_type,
        "road": road if road else None,
        "image_size": os.path.getsize(image_path),
        "vehicles": vehicles,
    }


# ─── Batch Analysis ──────────────────────────────────────────────────────────

async def run_analysis_cycle():
    """Analyse all camera images."""
    global _analysis_results, _analysis_summary, _last_analysis_time

    if not CAMERA_DIR.exists():
        log.warning("Camera directory not found")
        return

    images = list(CAMERA_DIR.glob("*.jpg"))
    if not images:
        log.warning("No camera images found")
        return

    start = time.time()
    results = {}

    for img_path in images:
        sensor_id = img_path.stem
        try:
            result = analyse_single_camera(sensor_id, img_path)
            results[sensor_id] = result
        except Exception as e:
            log.warning(f"Analysis failed for {sensor_id}: {e}")

    # Run Bravo vehicle detection on non-broken cameras
    bravo_targets = {}
    for sensor_id, r in results.items():
        if not r.get("broken", {}).get("broken") and not r.get("error"):
            # Find the image file for this sensor
            for pattern in [f"{sensor_id}.jpg", f"{sensor_id}_cam1.jpg", f"{sensor_id}_cam2.jpg"]:
                p = CAMERA_DIR / pattern
                if p.exists():
                    bravo_targets[sensor_id] = p
                    break

    await analyse_vehicles_bravo(bravo_targets)

    # Merge vehicle results into camera results
    for sensor_id, r in results.items():
        if sensor_id in _vehicle_results:
            r["vehicles"] = _vehicle_results[sensor_id]

    _analysis_results = results
    _last_analysis_time = time.time()
    elapsed = time.time() - start

    # Generate summary
    activities = [r["activity"]["activity_level"] for r in results.values()
                  if r.get("activity", {}).get("activity_level") not in (None, "unknown")]
    weathers = [r["weather"]["weather"] for r in results.values() if r.get("weather")]
    brightnesses = [r["brightness"]["mean_brightness"] for r in results.values() if r.get("brightness")]
    frozen = sum(1 for a in activities if a == "frozen")
    rain_cameras = sum(1 for r in results.values() if r.get("rain", {}).get("rain_detected"))
    rain_heavy = sum(1 for r in results.values() if r.get("rain", {}).get("lens_condition") == "rain_heavy")
    broken_cameras = sum(1 for r in results.values() if r.get("broken", {}).get("broken"))
    broken_reasons = {}
    for r in results.values():
        b = r.get("broken", {})
        if b.get("broken") and b.get("reason"):
            broken_reasons[b["reason"]] = broken_reasons.get(b["reason"], 0) + 1

    # Road type breakdown
    road_type_counts = {}
    for r in results.values():
        rt = r.get("road_type", {}).get("road_type", "unknown")
        road_type_counts[rt] = road_type_counts.get(rt, 0) + 1
    heavy = sum(1 for a in activities if a == "heavy")

    # Most common weather
    weather_counts = {}
    for w in weathers:
        weather_counts[w] = weather_counts.get(w, 0) + 1
    dominant_weather = max(weather_counts, key=weather_counts.get) if weather_counts else "unknown"

    _analysis_summary = {
        "total_analysed": len(results),
        "analysis_time_seconds": round(elapsed, 2),
        "frozen_cameras": frozen,
        "heavy_activity": heavy,
        "dominant_weather": dominant_weather,
        "avg_brightness": round(statistics.mean(brightnesses), 1) if brightnesses else 0,
        "activity_breakdown": {
            level: sum(1 for a in activities if a == level)
            for level in ["frozen", "static", "light", "moderate", "heavy"]
        },
        "weather_breakdown": weather_counts,
        "rain_detected_cameras": rain_cameras,
        "rain_heavy_cameras": rain_heavy,
        "broken_cameras": broken_cameras,
        "broken_reasons": broken_reasons,
        "road_types": road_type_counts,
        "last_analysis": datetime.now(timezone.utc).isoformat(),
    }

    # Vehicle detection summary
    total_vehicles = sum(
        r.get("vehicles", {}).get("total_vehicles", 0)
        for r in results.values()
    )
    cameras_with_vehicles = sum(
        1 for r in results.values()
        if r.get("vehicles", {}).get("total_vehicles", 0) > 0
    )
    density_breakdown = {}
    for r in results.values():
        d = r.get("vehicles", {}).get("density", "unknown")
        if d != "unknown":
            density_breakdown[d] = density_breakdown.get(d, 0) + 1

    vehicle_type_totals = {}
    for r in results.values():
        for vtype, count in r.get("vehicles", {}).get("vehicles", {}).items():
            vehicle_type_totals[vtype] = vehicle_type_totals.get(vtype, 0) + count

    _analysis_summary["total_vehicles"] = total_vehicles
    _analysis_summary["cameras_with_vehicles"] = cameras_with_vehicles
    _analysis_summary["density_breakdown"] = density_breakdown
    _analysis_summary["vehicle_types"] = vehicle_type_totals
    _analysis_summary["bravo_available"] = _bravo_available

    # Track incidents from Bravo
    all_incidents = {}
    for sid, r in results.items():
        v = r.get("vehicles", {})
        incs = v.get("incidents", [])
        if incs:
            all_incidents[sid] = incs
            _incidents[sid] = incs

    incident_count = sum(len(v) for v in all_incidents.values())
    _analysis_summary["incidents"] = {
        "total": incident_count,
        "cameras_with_incidents": len(all_incidents),
        "types": {},
    }
    for sid, incs in all_incidents.items():
        for inc in incs:
            t = inc["type"]
            _analysis_summary["incidents"]["types"][t] = _analysis_summary["incidents"]["types"].get(t, 0) + 1

    # Add scene descriptions to results
    for sid, desc in _scene_descriptions.items():
        if sid in results:
            results[sid]["scene_description"] = desc

    log.info(
        f"Analysis complete: {len(results)} cameras in {elapsed:.1f}s — "
        f"weather={dominant_weather}, frozen={frozen}, heavy={heavy}, rain={rain_cameras}, broken={broken_cameras}, "
        f"vehicles={total_vehicles} across {cameras_with_vehicles} cameras, incidents={incident_count}"
    )

    # Async post-processing: store to DB, check alerts, get descriptions
    await store_snapshots(results)
    await check_and_send_alerts(results)
    await fetch_scene_descriptions(results)


async def init_db():
    """Initialize database connection pool."""
    global _db_pool
    try:
        _db_pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)
        log.info("Database pool initialized")
    except Exception as e:
        log.error(f"Database connection failed: {e}")
        _db_pool = None


async def store_snapshots(results: dict):
    """Store analysis results in PostgreSQL."""
    if not _db_pool:
        return
    try:
        async with _db_pool.acquire() as conn:
            rows = []
            for sid, r in results.items():
                v = r.get("vehicles", {})
                veh = v.get("vehicles", {})
                rows.append((
                    sid,
                    v.get("total_vehicles", 0),
                    veh.get("car", 0),
                    veh.get("van", 0),
                    veh.get("truck", 0),
                    veh.get("bus", 0),
                    veh.get("motorcycle", 0),
                    v.get("persons", 0),
                    v.get("bicycles", 0),
                    v.get("density", "unknown"),
                    r.get("weather", {}).get("weather", "unknown"),
                    r.get("weather", {}).get("wet_road_score", 0),
                    r.get("brightness", {}).get("mean_brightness", 0),
                    r.get("brightness", {}).get("fog_likelihood", 0),
                    r.get("road_type", {}).get("road_type", "unknown"),
                    r.get("rain", {}).get("rain_detected", False),
                    r.get("broken", {}).get("broken", False),
                    v.get("inference_ms", 0),
                ))
            if rows:
                await conn.executemany(
                    """INSERT INTO traffic_snapshots
                    (sensor_id, total_vehicles, cars, vans, trucks, buses, motorcycles,
                     persons, bicycles, density, weather, wet_road_score, brightness,
                     fog_likelihood, road_type, rain_detected, broken, inference_ms)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)""",
                    rows
                )
                log.info(f"Stored {len(rows)} snapshots to database")
    except Exception as e:
        log.error(f"Failed to store snapshots: {e}")


async def check_and_send_alerts(results: dict):
    """Check for alert conditions and send to nuro hub."""
    global _active_alerts
    alerts_to_send = []

    for sid, r in results.items():
        v = r.get("vehicles", {})
        incidents = v.get("incidents", [])
        density = v.get("density", "")
        broken = r.get("broken", {}).get("broken", False)

        sensor_alerts = set()

        # Incident-based alerts
        for inc in incidents:
            alert_key = f"{sid}:{inc['type']}"
            sensor_alerts.add(inc["type"])
            if alert_key not in _active_alerts:
                alerts_to_send.append({
                    "sensor_id": sid,
                    "type": inc["type"],
                    "severity": inc["severity"],
                    "message": inc["message"],
                    "details": inc,
                })
                _active_alerts[alert_key] = True

        # Congestion alert
        if density == "congested":
            alert_key = f"{sid}:congestion"
            if alert_key not in _active_alerts:
                alerts_to_send.append({
                    "sensor_id": sid,
                    "type": "congestion",
                    "severity": "warning",
                    "message": f"Congested traffic: {v.get('total_vehicles', 0)} vehicles detected",
                })
                _active_alerts[alert_key] = True

        # Clear resolved alerts
        for key in list(_active_alerts.keys()):
            if key.startswith(f"{sid}:"):
                alert_type = key.split(":", 1)[1]
                if alert_type not in sensor_alerts and alert_type != "congestion":
                    del _active_alerts[key]
                elif alert_type == "congestion" and density != "congested":
                    del _active_alerts[key]

    # Store alerts in DB
    if alerts_to_send and _db_pool:
        try:
            async with _db_pool.acquire() as conn:
                for a in alerts_to_send:
                    await conn.execute(
                        """INSERT INTO traffic_alerts (sensor_id, alert_type, severity, message, details)
                        VALUES ($1, $2, $3, $4, $5)""",
                        a["sensor_id"], a["type"], a["severity"], a["message"],
                        json.dumps(a.get("details", {}))
                    )
        except Exception as e:
            log.error(f"Failed to store alerts: {e}")

    # Push to nuro hub
    if alerts_to_send:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{NURO_HUB}/api/alerts/batch",
                    json={
                        "source": SERVICE_ID,
                        "alerts": [{
                            "id": f"{a['sensor_id']}:{a['type']}",
                            "sensor_id": a["sensor_id"],
                            "type": a["type"],
                            "severity": a["severity"],
                            "message": a["message"],
                            "source": SERVICE_ID,
                        } for a in alerts_to_send]
                    }
                )
                log.info(f"Sent {len(alerts_to_send)} alerts to nuro")
        except Exception:
            pass  # nuro hub may not have batch endpoint yet

    if alerts_to_send:
        log.info(f"Alerts: {', '.join(a['type'] + '(' + a['sensor_id'] + ')' for a in alerts_to_send)}")


async def fetch_scene_descriptions(results: dict):
    """Get LLM scene descriptions from Bravo for cameras with notable activity."""
    global _scene_descriptions
    if not _bravo_available:
        return

    # Only describe cameras with incidents, congestion, or heavy traffic
    targets = {}
    for sid, r in results.items():
        v = r.get("vehicles", {})
        incidents = v.get("incidents", [])
        density = v.get("density", "")
        if incidents or density in ("heavy", "congested"):
            img_path = None
            for pattern in [f"{sid}.jpg", f"{sid}_cam1.jpg"]:
                p = CAMERA_DIR / pattern
                if p.exists():
                    img_path = p
                    break
            if img_path:
                targets[sid] = (img_path, r)

    if not targets:
        return

    async with httpx.AsyncClient(timeout=20) as client:
        described = 0
        for sid, (img_path, r) in list(targets.items())[:10]:  # Cap at 10 per cycle
            try:
                v = r.get("vehicles", {})
                det_json = json.dumps({
                    "vehicles": v.get("vehicles", {}),
                    "total_vehicles": v.get("total_vehicles", 0),
                    "persons": v.get("persons", 0),
                    "density": v.get("density", ""),
                    "incidents": v.get("incidents", []),
                })
                rt = r.get("road_type", {}).get("road_type", "")
                weather = r.get("weather", {}).get("weather", "")

                with open(img_path, "rb") as f:
                    resp = await client.post(
                        f"{BRAVO_VISION_URL}/api/describe",
                        files={"file": (f"{sid}.jpg", f, "image/jpeg")},
                        data={
                            "detection_json": det_json,
                            "road_type": rt,
                            "camera_name": sid,
                            "weather": weather,
                        }
                    )
                if resp.status_code == 200:
                    desc = resp.json().get("description", "")
                    if desc and not desc.startswith("["):
                        _scene_descriptions[sid] = desc
                        described += 1
            except Exception as e:
                log.debug(f"Scene description failed for {sid}: {e}")

        if described:
            log.info(f"Generated {described} scene descriptions")


async def analysis_loop():
    """Background loop running analysis every 5 minutes."""
    await init_db()
    await asyncio.sleep(10)  # Wait for cameras to be fetched
    while True:
        try:
            await run_analysis_cycle()
        except Exception as e:
            log.error(f"Analysis cycle failed: {e}")
        await asyncio.sleep(ANALYSIS_INTERVAL)


async def nuro_heartbeat():
    """Report to nuro hub."""
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                summary = _analysis_summary or {}
                await client.post(
                    f"{NURO_HUB}/api/services/heartbeat",
                    json={
                        "service": SERVICE_ID,
                        "status": "online",
                        "port": PORT,
                        "description": "Camera Vision — CCTV image analysis",
                        "url": f"http://localhost:{PORT}",
                        "stats": {
                            "cameras_analysed": summary.get("total_analysed", 0),
                            "dominant_weather": summary.get("dominant_weather", "unknown"),
                            "frozen_cameras": summary.get("frozen_cameras", 0),
                            "heavy_activity": summary.get("heavy_activity", 0),
                            "last_analysis": summary.get("last_analysis"),
                        },
                    },
                )
            except Exception:
                pass
            await asyncio.sleep(30)


# ─── FastAPI App ─────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(analysis_loop())
    asyncio.create_task(nuro_heartbeat())
    yield

app = FastAPI(title="Camera Vision", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/analysis")
async def get_analysis():
    """Full analysis results for all cameras."""
    return JSONResponse({
        "summary": _analysis_summary,
        "cameras": _analysis_results,
        "last_analysis": _last_analysis_time,
    })


@app.get("/api/analysis/summary")
async def get_summary():
    """Quick summary of latest analysis."""
    return JSONResponse(_analysis_summary or {"status": "no analysis yet"})


@app.get("/api/analysis/camera/{sensor_id}")
async def get_camera_analysis(sensor_id: str):
    """Get analysis for a specific camera sensor."""
    result = _analysis_results.get(sensor_id)
    if not result:
        return JSONResponse({"error": "No analysis for this sensor"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/analysis/alerts")
async def get_alerts():
    """Return cameras with notable conditions."""
    alerts = []
    for sensor_id, r in _analysis_results.items():
        issues = []
        if r.get("activity", {}).get("activity_level") == "frozen":
            issues.append({"type": "frozen", "severity": "warning", "detail": "Camera may be stuck — no scene change detected"})
        if r.get("activity", {}).get("activity_level") == "heavy":
            issues.append({"type": "congestion", "severity": "info", "detail": "Heavy activity detected — possible congestion"})
        if r.get("brightness", {}).get("fog_likelihood", 0) > 60:
            issues.append({"type": "fog", "severity": "warning", "detail": f"Fog likelihood: {r['brightness']['fog_likelihood']}%"})
        if r.get("broken", {}).get("broken"):
            reason = r["broken"]["reason"]
            issues.append({"type": "broken_camera", "severity": "error", "detail": f"Camera appears broken — {reason}"})
        if r.get("rain", {}).get("rain_detected"):
            condition = r["rain"]["lens_condition"]
            score = r["rain"]["rain_score"]
            issues.append({"type": "rain_on_lens", "severity": "warning" if condition == "rain_heavy" else "info", "detail": f"Rain on lens detected — {condition} (score: {score})"})
        if r.get("weather", {}).get("wet_road_score", 0) > 30:
            issues.append({"type": "wet_road", "severity": "info", "detail": f"Wet road indicators detected (score: {r['weather']['wet_road_score']})"})
        if issues:
            alerts.append({"sensor_id": sensor_id, "alerts": issues})
    return JSONResponse({"alerts": alerts, "total": len(alerts)})


@app.get("/api/nuro")
async def nuro_streams():
    """Nuro v2.0 streams format."""
    summary = _analysis_summary or {}
    activity = summary.get("activity_breakdown", {})
    weather = summary.get("dominant_weather", "unknown")

    weather_icon = {
        "sunny": "☀️", "clear_sky": "🔵", "overcast": "☁️",
        "mixed": "⛅", "dark": "🌙",
    }.get(weather, "🌤️")

    return JSONResponse({
        "version": "2.0",
        "label": "Camera Vision",
        "icon": "👁️",
        "description": f"CCTV analysis — {weather_icon} {weather}, {summary.get('total_analysed', 0)} cameras",
        "streams": [
            {
                "id": "weather",
                "label": "Conditions",
                "type": "stat",
                "value": weather,
                "unit": "",
                "color": "#3b82f6",
                "icon": weather_icon,
            },
            {
                "id": "activity_heavy",
                "label": "Busy Cameras",
                "type": "stat",
                "value": activity.get("heavy", 0),
                "unit": "cameras",
                "color": "#ef4444" if activity.get("heavy", 0) > 5 else "#22c55e",
            },
            {
                "id": "broken_cameras",
                "label": "Broken Cameras",
                "type": "stat",
                "value": summary.get("broken_cameras", 0),
                "unit": "cameras",
                "color": "#ef4444" if summary.get("broken_cameras", 0) > 0 else "#22c55e",
            },
            {
                "id": "rain_cameras",
                "label": "Rain on Lens",
                "type": "stat",
                "value": summary.get("rain_detected_cameras", 0),
                "unit": "cameras",
                "color": "#3b82f6" if summary.get("rain_detected_cameras", 0) > 0 else "#22c55e",
            },
            {
                "id": "activity_frozen",
                "label": "Frozen Sensors",
                "type": "stat",
                "value": summary.get("frozen_cameras", 0),
                "unit": "cameras",
                "color": "#ef4444" if summary.get("frozen_cameras", 0) > 3 else "#22c55e",
            },
            {
                "id": "avg_brightness",
                "label": "Avg Brightness",
                "type": "gauge",
                "value": summary.get("avg_brightness", 0),
                "unit": "",
                "min": 0,
                "max": 255,
                "color": "#f59e0b",
            },
            {
                "id": "road_types",
                "label": "Road Types",
                "type": "stat",
                "value": ", ".join(f"{v} {k}" for k, v in summary.get("road_types", {}).items() if k != "unknown"),
                "unit": "",
                "color": "#64748b",
            },
            {
                "id": "total_vehicles",
                "label": "Vehicles Detected",
                "type": "stat",
                "value": summary.get("total_vehicles", 0),
                "unit": "vehicles",
                "color": "#f97316" if summary.get("total_vehicles", 0) > 50 else "#22c55e",
            },
            {
                "id": "vehicle_types",
                "label": "Vehicle Types",
                "type": "stat",
                "value": ", ".join(f"{v} {k}s" for k, v in summary.get("vehicle_types", {}).items()),
                "unit": "",
                "color": "#64748b",
            },
            {
                "id": "traffic_density",
                "label": "Traffic Density",
                "type": "stat",
                "value": ", ".join(f"{v} {k}" for k, v in summary.get("density_breakdown", {}).items()),
                "unit": "",
                "color": "#ef4444" if summary.get("density_breakdown", {}).get("congested", 0) > 0 else "#22c55e",
            },
            {
                "id": "bravo_status",
                "label": "Bravo (YOLO)",
                "type": "stat",
                "value": "online" if summary.get("bravo_available") else "offline",
                "unit": "",
                "color": "#22c55e" if summary.get("bravo_available") else "#ef4444",
            },
            {
                "id": "incidents",
                "label": "Incidents",
                "type": "stat",
                "value": summary.get("incidents", {}).get("total", 0),
                "unit": "detected",
                "color": "#ef4444" if summary.get("incidents", {}).get("total", 0) > 0 else "#22c55e",
            },
            {
                "id": "analysis_speed",
                "label": "Analysis Time",
                "type": "stat",
                "value": summary.get("analysis_time_seconds", 0),
                "unit": "seconds",
                "color": "#8b5cf6",
            },
        ],
        "updated_at": summary.get("last_analysis", datetime.now(timezone.utc).isoformat()),
    })


@app.get("/api/analysis/history/{sensor_id}")
async def analysis_history(sensor_id: str, hours: int = 24):
    """Get historical traffic data for a sensor."""
    if not _db_pool:
        return JSONResponse({"error": "database not available"}, 503)
    try:
        async with _db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT captured_at, total_vehicles, cars, trucks, buses, vans,
                          persons, density, weather, brightness, wet_road_score
                FROM traffic_snapshots
                WHERE sensor_id = $1 AND captured_at > NOW() - INTERVAL '1 hour' * $2
                ORDER BY captured_at DESC
                LIMIT 500""",
                sensor_id, hours
            )
            return JSONResponse([dict(r) for r in rows], default=str)
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@app.get("/api/analysis/incidents")
async def analysis_incidents():
    """Get current active incidents across all cameras."""
    return JSONResponse({
        "incidents": {sid: incs for sid, incs in _incidents.items() if incs},
        "descriptions": _scene_descriptions,
        "active_alerts": list(_active_alerts.keys()),
    })


@app.get("/api/analysis/heatmap")
async def analysis_heatmap():
    """Get vehicle density data for map heatmap colouring."""
    heatmap = {}
    for sid, r in _analysis_results.items():
        v = r.get("vehicles", {})
        heatmap[sid] = {
            "total_vehicles": v.get("total_vehicles", 0),
            "density": v.get("density", "unknown"),
            "persons": v.get("persons", 0),
            "incidents": len(v.get("incidents", [])),
            "description": _scene_descriptions.get(sid, ""),
        }
    return JSONResponse(heatmap)


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_ID, "port": PORT}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("camera-vision:app", host="0.0.0.0", port=PORT, reload=False)
