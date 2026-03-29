# AyrTraffic 🗺️

**Live traffic map for Ayrshire, Scotland** — powered by nuro/WispAyr.

![AyrTraffic](https://img.shields.io/badge/AyrTraffic-Live-2563eb)
![Port](https://img.shields.io/badge/port-3870-green)

## Features

- 🗺️ **Interactive Leaflet.js map** centred on Ayr (dark/light theme)
- ⚠️ **Live incidents** — accidents, breakdowns, congestion (colour-coded)
- 🚧 **Roadworks overlay** — planned & emergency works
- 🚦 **Traffic flow heatmap** — congestion levels on A77, A78, A70, A71, A76, M77
- 🔄 **Auto-refresh** every 3 minutes
- 📊 **Filtering panel** — toggle layers on/off
- 🔍 **Search** — find roads and locations
- 📡 **nuro integration** — heartbeat + stats endpoint

## Data Sources

| Source | Type | Coverage |
|--------|------|----------|
| Traffic Scotland (traffic.gov.scot) | Incidents + Roadworks | Trunk roads |
| OpenStreetMap Overpass API | Road network + construction | All roads |
| Scottish Road Works Register (SRWR) | Roadworks | Local roads |
| Traffic Flow (synthetic + TomTom-ready) | Flow speeds | Major routes |

## Quick Start

```bash
cd /Users/noc/operations/ayrtraffic
mkdir -p logs

# Install dependencies
pip3 install fastapi uvicorn httpx feedparser --break-system-packages

# Run directly
python3 server.py

# Or via PM2
pm2 start ecosystem.config.js
```

Open: http://localhost:3870

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/traffic` | All traffic data (incidents + roadworks + flow) |
| `GET /api/traffic/incidents` | Incidents only |
| `GET /api/traffic/roadworks` | Roadworks only |
| `GET /api/traffic/flow` | Traffic flow only |
| `GET /api/nuro` | nuro stats (congestion score, counts) |
| `POST /api/refresh` | Force data refresh |
| `GET /health` | Health check |

### nuro Stats Response
```json
{
  "service": "ayrtraffic",
  "status": "online",
  "total_incidents": 3,
  "active_roadworks": 8,
  "congestion_score": 42,
  "sources_active": 4,
  "last_updated": "2026-03-29T22:00:00Z",
  "url": "http://localhost:3870"
}
```

## Configuration

Edit `server.py`:
- `PORT` — default 3870
- `CACHE_TTL` — cache lifetime in seconds (default 180)
- `NURO_HUB` — nuro hub URL
- `AYRSHIRE_BBOX` — geographic filter bounds

## Tech Stack

- **Backend:** Python FastAPI + uvicorn
- **Frontend:** Vanilla HTML/CSS/JS + Leaflet.js
- **Tiles:** CartoDB Positron (dark/light)
- **Cache:** SQLite
- **Deploy:** PM2

## WispAyr

Part of the WispAyr nuro ecosystem. Visit [wispayr.com](https://wispayr.com).
