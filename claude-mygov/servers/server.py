#!/usr/bin/env python3
"""MCP server exposing Malaysia Government Open API (api.data.gov.my) as tools.

Dependency-free: speaks the MCP stdio JSON-RPC protocol directly (initialize,
tools/list, tools/call) using only the Python standard library. Register with
Claude Code via:  claude mcp add -s user mygov -- python3 <this file>

API reference: https://developer.data.gov.my  (base: https://api.data.gov.my)
Rate limit: 4 req/min per API family — the server keeps a per-family throttle.
"""
import json
import re
import sys
import urllib.request
import urllib.parse
import struct
import time
import zipfile
import io
from collections import defaultdict

BASE = "https://api.data.gov.my"

# ---- minimal GTFS-realtime protobuf wire parser (subset we need) ----
def _read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7

def _parse_feed_message(data):
    """Parse GTFS-realtime FeedMessage: entity[] -> {id, lat, lon, timestamp}."""
    vehicles = []
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:  # varint
            _, pos = _read_varint(data, pos)
        elif wire == 1:  # 64-bit
            pos += 8
        elif wire == 2:  # length-delimited
            ln, pos = _read_varint(data, pos)
            payload = data[pos:pos + ln]
            pos += ln
            if field == 2:  # entity
                ent = _parse_feed_entity(payload)
                if ent:
                    vehicles.append(ent)
        elif wire == 5:  # 32-bit
            pos += 4
        else:
            break
    return vehicles

def _parse_feed_entity(data):
    ent = {"id": None, "lat": None, "lon": None, "timestamp": None}
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, pos = _read_varint(data, pos)
            payload = data[pos:pos + ln]
            pos += ln
            if field == 1:
                ent["id"] = payload.decode("utf-8", "replace")
            elif field == 8:  # vehicle -> VehiclePosition
                vp = _parse_vehicle_position(payload)
                if vp:
                    ent.update(vp)
        elif wire == 0:
            _, pos = _read_varint(data, pos)
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return ent

def _parse_vehicle_position(data):
    vp = {}
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, pos = _read_varint(data, pos)
            payload = data[pos:pos + ln]
            pos += ln
            if field == 2:  # position -> Position
                p = _parse_position(payload)
                if p:
                    vp.update(p)
        elif wire == 0:
            val, pos = _read_varint(data, pos)
            if field == 6:
                vp["timestamp"] = val
        elif wire == 5:
            if field == 5:
                pass
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return vp

def _parse_position(data):
    p = {}
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 5:  # float32 lat/lon
            val = struct.unpack("<f", data[pos:pos + 4])[0]
            pos += 4
            if field == 1:
                p["lat"] = round(val, 6)
            elif field == 2:
                p["lon"] = round(val, 6)
            elif field == 3:
                p["bearing"] = round(val, 1)
            elif field == 5:
                p["speed"] = round(val, 1)
        elif wire == 0:
            _, pos = _read_varint(data, pos)
        elif wire == 2:
            ln, pos = _read_varint(data, pos)
            pos += ln
        elif wire == 1:
            pos += 8
        else:
            break
    return p

# ---- API fetch helpers ----
class Throttle:
    def __init__(self):
        self._hits = defaultdict(list)

    def wait(self, family):
        now = time.time()
        self._hits[family] = [t for t in self._hits[family] if now - t < 60]
        if len(self._hits[family]) >= 4:
            sleep_for = 60 - (now - self._hits[family][0]) + 0.2
            time.sleep(sleep_for)
        self._hits[family].append(time.time())

THROTTLE = Throttle()


def api_get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 mygov-mcp"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    if "json" in ctype or path.startswith(("/data-catalogue", "/opendosm", "/weather")):
        return json.loads(body.decode("utf-8"))
    return body  # binary (GTFS zip / protobuf)


def get_weather_forecast(location=None):
    THROTTLE.wait("weather")
    params = {"limit": 200}
    if location:
        params["contains"] = f"{location}@location__location_name"
    return api_get("/weather/forecast", params)


def get_weather_warning():
    THROTTLE.wait("weather")
    return api_get("/weather/warning")


def get_data_catalogue(dataset_id, limit=100, filters=None):
    THROTTLE.wait("data-catalogue")
    params = {"id": dataset_id, "limit": limit}
    if filters:
        params.update(filters)
    return api_get("/data-catalogue", params)


def get_opendosm(dataset_id, limit=100, filters=None):
    THROTTLE.wait("opendosm")
    params = {"id": dataset_id, "limit": limit}
    if filters:
        params.update(filters)
    return api_get("/opendosm", params)


def get_gtfs_static_summary(agency):
    """Download GTFS static ZIP and summarize routes/stops/trips."""
    THROTTLE.wait("gtfs")
    if not re.match(r"^[a-z0-9-]{1,32}$", agency):
        return {"error": "invalid agency"}
    if agency.startswith("prasarana"):
        data = api_get(f"/gtfs-static/{agency}?category=rapid-bus-kl")
    else:
        data = api_get(f"/gtfs-static/{agency}")
    z = zipfile.ZipFile(io.BytesIO(data))
    summary = {"agency": agency, "files": z.namelist()}
    for fname in ("routes.txt", "stops.txt", "trips.txt"):
        if fname in z.namelist():
            lines = z.read(fname).decode("utf-8", "replace").splitlines()
            header = lines[0].split(",")
            summary[f"{fname}_rows"] = len(lines) - 1
            if fname == "routes.txt" and len(lines) > 1:
                summary["sample_routes"] = [
                    dict(zip(header, lines[i].split(",")))
                    for i in range(1, min(6, len(lines)))
                ]
    return summary


def get_gtfs_realtime(agency, category=None):
    THROTTLE.wait("gtfs")
    if not re.match(r"^[a-z0-9-]{1,32}$", agency):
        return {"error": "invalid agency"}
    if category and not re.match(r"^[a-z0-9-]{1,32}$", category):
        return {"error": "invalid category"}
    path = f"/gtfs-realtime/vehicle-position/{agency}"
    if category:
        path += f"?category={category}"
    data = api_get(path)
    vehicles = _parse_feed_message(data)
    return {
        "agency": agency,
        "live_vehicles": len(vehicles),
        "vehicles": vehicles[:100],
    }


# ---- Rapid KL live bus feed (myrapidbus kiosk data source) ----
# The api.data.gov.my GTFS-RT feed for prasarana is frequently empty, but the
# official kiosk (myrapidbus.prasarana.com.my/kiosk) shows live buses from a
# socket.io server (rapidbus-socketio-avl.prasarana.com.my). socket.io's
# engine.io polling transport is plain HTTP, so it can be consumed without a
# websocket client:
#   1. GET  /socket.io/?EIO=4&transport=polling   -> 0{"sid":...}
#   2. POST 40{...} connect, POST 42["onFts-reload",{...}] emit
#   3. GET  poll -> 42["onFts-client","<base64(gzip(json))>"]
RAPID_SID = "m0ckulfr515l5s79sgd2hhva9iqm3cr2"  # shared kiosk sid
RAPID_URL = "https://rapidbus-socketio-avl.prasarana.com.my/socket.io/"


def _rapid_post(url, payload):
    req = urllib.request.Request(url, data=payload.encode("utf-8"),
                                 headers={"Content-Type": "text/plain;charset=UTF-8",
                                          "User-Agent": "Mozilla/5.0 mygov-mcp"})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def get_rapid_bus_live(provider="RKL", route=""):
    t = int(time.time() * 1000)
    with urllib.request.urlopen(f"{RAPID_URL}?EIO=4&transport=polling&t={t}",
                                timeout=20) as r:
        open_text = r.read().decode("utf-8", "replace")
    m = re.search(r'^0\{"sid":"([^"]+)"', open_text)
    if not m:
        return {"error": "handshake_failed", "raw": open_text[:80]}
    sid = m.group(1)
    base = f"{RAPID_URL}?EIO=4&transport=polling&sid={sid}"
    _rapid_post(f"{base}&t={int(time.time()*1000)}",
                f'40{{"sid":"{RAPID_SID}","uid":""}}')
    _rapid_post(f"{base}&t={int(time.time()*1000)}",
                f'42["onFts-reload",{{"sid":"{RAPID_SID}","uid":"",'
                f'"provider":"{provider}","route":"{route}"}}]')
    time.sleep(1.5)
    with urllib.request.urlopen(f"{base}&t={int(time.time()*1000)}",
                                timeout=20) as r:
        poll_text = r.read().decode("utf-8", "replace")
    payload = None
    for frame in poll_text.split("\x1e"):
        fm = re.match(r'^42\["onFts-client","(.*)"\]$', frame, re.S)
        if fm:
            payload = fm.group(1)
            break
    if not payload:
        return {"error": "no_data_frame", "raw": poll_text[:80]}
    import base64
    import gzip as _gzip
    jdata = json.loads(_gzip.decompress(base64.b64decode(payload)).decode("utf-8"))
    buses = jdata if isinstance(jdata, list) else []
    return {
        "provider": provider,
        "route": route or "all",
        "live_buses": len(buses),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "buses": [
            {
                "bus_no": b.get("bus_no"), "latitude": b.get("latitude"),
                "longitude": b.get("longitude"), "route": b.get("route"),
                "dir": b.get("dir"), "speed": b.get("speed"),
                "angle": b.get("angle"), "dt_gps": b.get("dt_gps"),
                "trip_no": b.get("trip_no"), "accessibility": b.get("accessibility"),
            }
            for b in buses[:200]
        ],
    }


# ---- MCP protocol (stdio JSON-RPC 2.0) ----
def get_flood_risk():
    """Live flood risk from JPS telemetry, via the dashboard's /api/flood proxy.

    The proxy fetches JPS's ~1.3 MB gauge feed server-side, keeps only
    danger/warning/alert stations with a reading in the last 24h (dead gauges
    excluded), and slims each station to name/coords/level/trend/timestamp.
    """
    t = int(time.time())
    req = urllib.request.Request(
        f"https://malaysia-at-a-glance.com/api/flood?cb={t}",
        headers={"User-Agent": "mygov-mcp/1.0 (+https://malaysia-at-a-glance.com)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return {
        "updated": data.get("updated"),
        "at_risk": data.get("at_risk"),
        "states": data.get("states", []),
        "stations": data.get("stations", []),
    }


def get_pricecatcher(item="", group="", limit=20):
    """PriceCatcher grocery price index (KPDN, 198-item basket, daily)."""
    req = urllib.request.Request(
        "https://malaysia-at-a-glance.com/prices.json",
        headers={"User-Agent": "mygov-mcp/1.0 (+https://malaysia-at-a-glance.com)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    q = str(item or "").strip().lower()
    grp = str(group or "").strip().upper()
    try:
        lim = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        lim = 20
    items = data.get("items") or []
    if q:
        items = [it for it in items if q in str(it.get("n", "")).lower()]
    if grp:
        items = [it for it in items if str(it.get("g", "")) == grp]
    items = items[:lim]
    out = []
    for it in items:
        p = it.get("p") or []
        months = data.get("months") or []
        out.append({
            "item": it.get("n"), "unit": it.get("u"), "group": it.get("g"),
            "kind": it.get("k"),
            "latest_price": p[-1] if p else None,
            "mom_pct": it.get("mom"), "yoy_pct": it.get("yoy"),
            "price_history": [{"month": months[i], "price": v}
                              for i, v in enumerate(p) if i < len(months)],
        })
    basket = data.get("basket") or {}
    return {
        "generated": data.get("generated"), "as_of": data.get("asOf"),
        "months": data.get("months"),
        "basket": {"n": basket.get("n"), "base": basket.get("base"),
                   "national_index": basket.get("national")} if basket else None,
        "items": out,
    }


def get_tourism(country="", limit=10):
    """Monthly visitor arrivals (Tourism Malaysia, top 51, ~1 month lag)."""
    req = urllib.request.Request(
        "https://malaysia-at-a-glance.com/tourism.json",
        headers={"User-Agent": "mygov-mcp/1.0 (+https://malaysia-at-a-glance.com)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    q = str(country or "").strip().lower()
    try:
        lim = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        lim = 10
    rows = data.get("visitor") or []
    if q:
        rows = [r for r in rows if q in str(r.get("country", "")).lower()]
    rows = rows[:lim]
    out = [{
        "rank": r.get("rank"), "country": r.get("country"),
        "arrivals": r.get("cur"), "prev_month": r.get("prev"),
        "yoy_pct": r.get("g_yoy"), "vs_2019_pct": r.get("g_2019"),
        "mom_pct": r.get("g_mom"), "ytd_arrivals": r.get("ytd26"),
        "ytd_yoy_pct": r.get("gy_yoy"),
    } for r in rows]
    return {
        "as_of": data.get("asOf"), "generated": data.get("generated"),
        "totals": data.get("totals"),
        "countries": out,
    }


def get_rapid_service_alert():
    """Latest Rapid KL service alert (myrapid.com.my PULSE).

    The source is behind Incapsula (a JS-challenge WAF; its wp-json also
    returns 401 for anonymous reads), so the dashboard's collect_rapid
    workflow scrapes it via the r.jina.ai reader every 10 min and publishes
    the newest post as rapid_alerts.json. This tool returns that file - the
    same data the dashboard's alert deck shows: one card, latest post only.
    """
    req = urllib.request.Request(
        "https://malaysia-at-a-glance.com/rapid_alerts.json",
        headers={"User-Agent": "mygov-mcp/1.0 (+https://malaysia-at-a-glance.com)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    latest = data.get("latest") or {}
    return {
        "updated": data.get("updated"),
        "title": latest.get("title"),
        "excerpt": latest.get("excerpt"),
        "url": latest.get("url"),
        "posted_epoch": latest.get("ts"),
    }


def get_air_quality():
    """Live air quality index for 18 major Malaysian cities.

    Polls Open-Meteo's hourly air-quality model (free, keyless - the
    official APIMS feed blocks non-browser clients) via the dashboard's
    /api/aqi proxy. Returns every city's US AQI and PM2.5, worst first,
    plus the cleanest station for comparison. US AQI 101+ is the haze
    alert threshold (Unhealthy).
    """
    t = int(time.time())
    req = urllib.request.Request(
        f"https://malaysia-at-a-glance.com/api/aqi?cb={t}",
        headers={"User-Agent": "mygov-mcp/1.0 (+https://malaysia-at-a-glance.com)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    stations = data.get("stations") or []
    return {
        "updated": data.get("updated"),
        "reading_time": data.get("reading_time"),
        "worst": data.get("worst"),
        "cleanest": data.get("cleanest"),
        "stations": stations,
    }


TOOLS = [
    {
        "name": "mygov_weather_forecast",
        "description": "7-day weather forecast for Malaysia locations (MET Malaysia). "
                       "Optionally filter by location name (e.g. 'Kota Bharu', 'Langkawi'). "
                       "Returns date, morning/afternoon/night forecast, min/max temp.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Optional location name filter"},
                "limit": {"type": "integer", "description": "Max records (default 200)"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_weather_warning",
        "description": "Active weather warnings for Malaysia (MET Malaysia).",
        "inputSchema": {"type": "object", "properties": {}},

        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_data_catalogue",
        "description": "Query the Data Catalogue API (general gov datasets). Known ids: "
                       "fuelprice (RON95/RON97/diesel weekly). Filters use value@column syntax, "
                       "e.g. {'filter': 'level@series_type'} or {'sort': '-date'}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "Dataset id, e.g. fuelprice"},
                "limit": {"type": "integer", "description": "Max records (default 100)"},
                "filter": {"type": "string", "description": "value@column exact match"},
                "contains": {"type": "string", "description": "value@column partial match"},
                "sort": {"type": "string", "description": "column or -column"},
                "date_start": {"type": "string", "description": "YYYY-MM-DD@date"},
                "date_end": {"type": "string", "description": "YYYY-MM-DD@date"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_opendosm",
        "description": "Query the OpenDOSM API (DOSM economics/statistics). Known ids: "
                       "cpi_core (CPI index). Supports same filters as data catalogue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "Dataset id, e.g. cpi_core"},
                "limit": {"type": "integer", "description": "Max records (default 100)"},
                "filter": {"type": "string", "description": "value@column exact match"},
                "sort": {"type": "string", "description": "column or -column"},
                "date_start": {"type": "string", "description": "YYYY-MM-DD@date"},
                "date_end": {"type": "string", "description": "YYYY-MM-DD@date"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_gtfs_static_summary",
        "description": "Download a GTFS static schedule ZIP (ktmb, prasarana, mybas-kota-bharu, "
                       "mybas-alor-setar, mybas-kuala-terengganu, mybas-johor-bahru, etc.) and "
                       "return file list, row counts, and sample routes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agency": {"type": "string", "description": "e.g. ktmb, prasarana, mybas-kota-bharu"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_gtfs_realtime",
        "description": "Live vehicle positions (GTFS-realtime protobuf). Agencies: ktmb, "
                       "prasarana (category rapid-bus-kl / rapid-rail-kl), mybas-*. "
                       "Returns count + vehicle id/lat/lon list. NOTE: the prasarana "
                       "GTFS-RT feed is often empty - use mygov_rapid_bus_live for "
                       "actual Rapid KL bus positions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agency": {"type": "string", "description": "ktmb, prasarana, or mybas-*"},
                "category": {"type": "string", "description": "For prasarana: rapid-bus-kl, rapid-rail-kl, ..."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_rapid_bus_live",
        "description": "Live Rapid KL / Rapid Penang / Rapid Kuantan bus positions from the "
                       "official myrapidbus kiosk feed (800+ buses). Providers: RKL (Klang "
                       "Valley), RPG (Penang), RKN (Kuantan). Optional route filter "
                       "(e.g. T2000, 300). Returns bus_no, lat/lon, route, speed, direction, "
                       "last GPS time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "RKL, RPG, or RKN (default RKL)"},
                "route": {"type": "string", "description": "Optional route number filter"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_flood_risk",
        "description": "Live flood risk from JPS (Department of Irrigation and Drainage) "
                       "water-level telemetry. Returns stations currently at "
                       "danger/warning/alert (only gauges that reported within the last "
                       "24h - dead gauges excluded), each with name, state, district, "
                       "lat/lon, water level, danger threshold, trend and last reading "
                       "time, plus a per-state count summary.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_pricecatcher",
        "description": "Malaysia grocery price index (KPDN PriceCatcher, 198-item "
                       "basket). Search items by name (e.g. TOMATO, RICE, ONION) or "
                       "filter by group (BARANGAN SEGAR, MAKANAN KERING, MINUMAN...). "
                       "Returns each item's current price, unit, month-on-month and "
                       "year-on-year change, plus the 13-month price history. Updated "
                       "daily by the dashboard's PriceCatcher collector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item name substring search (case-insensitive)"},
                "group": {"type": "string", "description": "Optional item group filter, e.g. BARANGAN SEGAR"},
                "limit": {"type": "integer", "description": "Max items to return (default 20, max 1000)"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_tourism_arrivals",
        "description": "Malaysia monthly international visitor arrivals by country "
                       "of nationality (Tourism Malaysia, top 51). Returns the "
                       "month's total, month-on-month and year-on-year growth vs "
                       "2025 and 2019, plus the year-to-date picture. Optional "
                       "country filter (e.g. SINGAPORE, CHINA) and limit. Data "
                       "updates monthly (~1 month lag); use for tourism demand, "
                       "recovery vs pre-pandemic 2019, and top source markets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "description": "Country/nationality filter, case-insensitive substring (e.g. SINGAPORE, CHINA, INDIA)"},
                "limit": {"type": "integer", "description": "Max countries to return (default 10, max 100)"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_rapid_service_alert",
        "description": "Latest Rapid KL service alert (LRT/MRT/monorail/bus disruption, myrapid.com.my "
                       "PULSE). Returns the newest post only: title, excerpt, link, posted time. "
                       "Source is behind Incapsula; collected via the dashboard every 10 min.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
    {
        "name": "mygov_air_quality",
        "description": "Live air quality index (US AQI) for 18 major Malaysian cities "
                       "(Open-Meteo hourly model). Returns every city's AQI and PM2.5 sorted "
                       "worst-first, plus the cleanest station for comparison. US AQI 101+ "
                       "(Unhealthy) is the haze alert threshold.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
    },
]


def rpc_response(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def call_tool(name, args):
    a = args or {}
    # Clamp client-supplied limits - an arbitrary huge value would force a
    # giant upstream fetch.
    def _clamp(v, default):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        return max(1, min(n, 1000))
    if name == "mygov_weather_forecast":
        return get_weather_forecast(a.get("location"))
    if name == "mygov_weather_warning":
        return get_weather_warning()
    if name == "mygov_data_catalogue":
        filters = {k: v for k, v in a.items()
                   if k in ("filter", "contains", "sort", "date_start", "date_end") and v}
        return get_data_catalogue(a.get("dataset_id", ""), _clamp(a.get("limit"), 100), filters)
    if name == "mygov_opendosm":
        filters = {k: v for k, v in a.items()
                   if k in ("filter", "sort", "date_start", "date_end") and v}
        return get_opendosm(a.get("dataset_id", ""), _clamp(a.get("limit"), 100), filters)
    if name == "mygov_gtfs_static_summary":
        return get_gtfs_static_summary(a.get("agency", "ktmb"))
    if name == "mygov_gtfs_realtime":
        return get_gtfs_realtime(a.get("agency", "ktmb"), a.get("category"))
    if name == "mygov_rapid_bus_live":
        provider = str(a.get("provider", "RKL")).upper()
        if provider not in ("RKL", "RPG", "RKN"):
            raise ValueError(f"unknown provider {provider} (use RKL, RPG, or RKN)")
        route = str(a.get("route", ""))
        if route and not re.match(r"^[A-Za-z0-9-]{1,16}$", route):
            raise ValueError("invalid route")
        return get_rapid_bus_live(provider, route)
    if name == "mygov_flood_risk":
        return get_flood_risk()
    if name == "mygov_pricecatcher":
        return get_pricecatcher(a.get("item", ""), a.get("group", ""),
                                a.get("limit", 20))
    if name == "mygov_tourism_arrivals":
        return get_tourism(a.get("country", ""), a.get("limit", 10))
    if name == "mygov_rapid_service_alert":
        return get_rapid_service_alert()
    if name == "mygov_air_quality":
        return get_air_quality()
    raise ValueError(f"Unknown tool: {name}")


def main():
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            initialized = True
            sys.stdout.write(json.dumps(rpc_response(mid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mygov-api-mcp", "version": "1.0.0"},
            })) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            sys.stdout.write(json.dumps(rpc_response(mid, {"tools": TOOLS})) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            try:
                result = call_tool(name, args)
                content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]
                sys.stdout.write(json.dumps(rpc_response(mid, {
                    "content": content,
                    "isError": False,
                })) + "\n")
            except Exception as e:
                sys.stdout.write(json.dumps(rpc_error(mid, -32000, f"{type(e).__name__}: {e}")) + "\n")
            sys.stdout.flush()
        elif method == "ping":
            sys.stdout.write(json.dumps(rpc_response(mid, {})) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
