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
import socket
import sys
import urllib.error
import urllib.request
import urllib.parse
import struct
import time
import zipfile
import io
from collections import defaultdict

BASE = "https://api.data.gov.my"
DASH = "https://malaysia-at-a-glance.com"
UA = "mygov-mcp/1.1 (+https://malaysia-at-a-glance.com)"


# ---- structured tool errors ----
# Agents react far better to a machine-readable code than to a stringified
# Python exception, so every failure path raises ToolError and the tools/call
# handler serializes it as an isError result with a stable code.
class ToolError(Exception):
    def __init__(self, code, message, retryable=False, retry_after_seconds=None,
                 details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.details = details

    def to_dict(self):
        err = {"code": self.code, "message": self.message,
               "retryable": self.retryable}
        if self.retry_after_seconds is not None:
            err["retry_after_seconds"] = self.retry_after_seconds
        if self.details:
            err["details"] = self.details
        return {"error": err}


def invalid(message, **details):
    return ToolError("INVALID_ARGUMENT", message, details=details or None)


def _upstream_error(exc, url):
    """Map a urllib/socket failure onto a stable ToolError code."""
    host = urllib.parse.urlsplit(url).netloc
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return ToolError("NOT_FOUND", f"{host} has no data at this path",
                             details={"status": exc.code, "url": url})
        if exc.code == 429:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry = int(retry)
            except (TypeError, ValueError):
                retry = 60
            return ToolError("UPSTREAM_RATE_LIMIT",
                             f"{host} rate-limited this request",
                             retryable=True, retry_after_seconds=retry,
                             details={"status": exc.code})
        return ToolError("UPSTREAM_UNAVAILABLE",
                         f"{host} returned HTTP {exc.code}",
                         retryable=exc.code >= 500,
                         retry_after_seconds=30 if exc.code >= 500 else None,
                         details={"status": exc.code, "url": url})
    if isinstance(exc, socket.timeout) or isinstance(
            getattr(exc, "reason", None), socket.timeout):
        return ToolError("UPSTREAM_TIMEOUT", f"{host} did not respond in time",
                         retryable=True, retry_after_seconds=15,
                         details={"url": url})
    return ToolError("UPSTREAM_UNAVAILABLE", f"could not reach {host}: {exc}",
                     retryable=True, retry_after_seconds=30,
                     details={"url": url})


def http_get(url, headers=None, timeout=30):
    """GET returning raw bytes, with upstream failures mapped to ToolError."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise _upstream_error(e, url)


def http_get_json(url, headers=None, timeout=30):
    body, _ = http_get(url, headers, timeout)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise ToolError("DATA_UNAVAILABLE",
                        f"upstream returned a non-JSON body: {e}",
                        retryable=True, retry_after_seconds=30,
                        details={"url": url})


# ---- provenance ----
# Every tool result is wrapped as {"data": ..., "meta": {...}} so a client can
# cite the publisher, and can tell when the data is *from* apart from when it
# was fetched. freshness is one of: live | daily | monthly | quarterly | static.
SOURCES = {
    "mygov_weather_forecast": {
        "source": "Malaysian Meteorological Department (MET Malaysia)",
        "source_url": "https://api.data.gov.my/weather/forecast",
        "dataset": "weather/forecast", "freshness": "daily",
        "update_frequency": "daily", "max_age_seconds": 21600},
    "mygov_weather_warning": {
        "source": "Malaysian Meteorological Department (MET Malaysia)",
        "source_url": "https://api.data.gov.my/weather/warning",
        "dataset": "weather/warning", "freshness": "live",
        "update_frequency": "as issued", "max_age_seconds": 900},
    "mygov_data_catalogue": {
        "source": "Malaysia Government Open Data (data.gov.my)",
        "source_url": "https://api.data.gov.my/data-catalogue",
        "freshness": "varies", "update_frequency": "per dataset"},
    "mygov_opendosm": {
        "source": "Department of Statistics Malaysia (DOSM), OpenDOSM",
        "source_url": "https://api.data.gov.my/opendosm",
        "freshness": "varies", "update_frequency": "per dataset"},
    "mygov_gtfs_static_summary": {
        "source": "data.gov.my GTFS static feeds",
        "source_url": "https://api.data.gov.my/gtfs-static",
        "freshness": "static", "update_frequency": "irregular",
        "max_age_seconds": 86400},
    "mygov_gtfs_realtime": {
        "source": "data.gov.my GTFS-realtime vehicle positions",
        "source_url": "https://api.data.gov.my/gtfs-realtime/vehicle-position",
        "freshness": "live", "update_frequency": "~30s", "max_age_seconds": 60},
    "mygov_rapid_bus_live": {
        "source": "Prasarana myrapidbus kiosk AVL feed",
        "source_url": "https://myrapidbus.prasarana.com.my/kiosk",
        "freshness": "live", "update_frequency": "~15s", "max_age_seconds": 60},
    "mygov_flood_risk": {
        "source": "Department of Irrigation and Drainage (JPS) telemetry",
        "source_url": "https://publicinfobanjir.water.gov.my",
        "freshness": "live", "update_frequency": "~15 min",
        "max_age_seconds": 900},
    "mygov_pricecatcher": {
        "source": "Ministry of Domestic Trade and Cost of Living (KPDN), PriceCatcher",
        "source_url": "https://open.dosm.gov.my/data-catalogue/pricecatcher",
        "dataset": "pricecatcher", "freshness": "daily",
        "update_frequency": "daily", "max_age_seconds": 86400},
    "mygov_tourism_arrivals": {
        "source": "Tourism Malaysia",
        "source_url": "https://data.tourism.gov.my",
        "freshness": "monthly", "update_frequency": "monthly (~1 month lag)",
        "max_age_seconds": 86400},
    "mygov_rapid_service_alert": {
        "source": "Rapid Rail / Rapid Bus service alerts (myrapid.com.my PULSE)",
        "source_url": "https://myrapid.com.my/pulse",
        "freshness": "live", "update_frequency": "~10 min",
        "max_age_seconds": 900},
    "mygov_air_quality": {
        "source": "Open-Meteo air-quality model (APIMS-equivalent, US AQI scale)",
        "source_url": "https://open-meteo.com/en/docs/air-quality-api",
        "freshness": "live", "update_frequency": "hourly",
        "max_age_seconds": 3600},
    "mygov_hotel_performance": {
        "source": "Tourism Malaysia, Paid Accommodation Survey",
        "source_url": "https://data.tourism.gov.my",
        "freshness": "quarterly", "update_frequency": "quarterly",
        "max_age_seconds": 86400},
    "mygov_election_results": {
        "source": "Suruhanjaya Pilihan Raya Malaysia (SPR)",
        "source_url": "https://keputusan.spr.gov.my",
        "freshness": "static", "update_frequency": "per election"},
}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def envelope(tool, data, data_period=None, data_updated_at=None, **extra):
    """Wrap a tool payload with provenance.

    retrieved_at is when *we* called the API; data_updated_at is when the
    publisher last refreshed it and data_period is what the numbers describe.
    Conflating them makes an agent report month-old figures as "today's".
    """
    meta = dict(SOURCES.get(tool, {}))
    meta["retrieved_at"] = now_iso()
    if data_period is not None:
        meta["data_period"] = data_period
    if data_updated_at is not None:
        meta["data_updated_at"] = data_updated_at
    meta.update({k: v for k, v in extra.items() if v is not None})
    return {"data": data, "meta": meta}

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
    body, headers = http_get(url, headers={"User-Agent": "Mozilla/5.0 mygov-mcp"})
    ctype = headers.get("Content-Type", "")
    if "json" in ctype or path.startswith(("/data-catalogue", "/opendosm", "/weather")):
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ToolError("DATA_UNAVAILABLE",
                            f"data.gov.my returned a non-JSON body: {e}",
                            retryable=True, retry_after_seconds=30,
                            details={"url": url})
    return body  # binary (GTFS zip / protobuf)


def get_weather_forecast(location=None, limit=50):
    THROTTLE.wait("weather")
    params = {"limit": limit}
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
        raise invalid("agency must be lowercase letters, digits or hyphens",
                      agency=agency)
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


def get_gtfs_realtime(agency, category=None, limit=50):
    THROTTLE.wait("gtfs")
    if not re.match(r"^[a-z0-9-]{1,32}$", agency):
        raise invalid("agency must be lowercase letters, digits or hyphens",
                      agency=agency)
    if category and not re.match(r"^[a-z0-9-]{1,32}$", category):
        raise invalid("category must be lowercase letters, digits or hyphens",
                      category=category)
    path = f"/gtfs-realtime/vehicle-position/{agency}"
    if category:
        path += f"?category={category}"
    data = api_get(path)
    vehicles = _parse_feed_message(data)
    return {
        "agency": agency,
        "live_vehicles": len(vehicles),
        "returned": min(len(vehicles), limit),
        "truncated": len(vehicles) > limit,
        "vehicles": vehicles[:limit],
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
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise _upstream_error(e, url)


def get_rapid_bus_live(provider="RKL", route="", limit=50):
    t = int(time.time() * 1000)
    handshake_url = f"{RAPID_URL}?EIO=4&transport=polling&t={t}"
    open_text = http_get(handshake_url, timeout=20)[0].decode("utf-8", "replace")
    m = re.search(r'^0\{"sid":"([^"]+)"', open_text)
    if not m:
        raise ToolError("UPSTREAM_UNAVAILABLE",
                        "myrapidbus kiosk feed did not complete the handshake",
                        retryable=True, retry_after_seconds=15,
                        details={"raw": open_text[:80]})
    sid = m.group(1)
    base = f"{RAPID_URL}?EIO=4&transport=polling&sid={sid}"
    _rapid_post(f"{base}&t={int(time.time()*1000)}",
                f'40{{"sid":"{RAPID_SID}","uid":""}}')
    _rapid_post(f"{base}&t={int(time.time()*1000)}",
                f'42["onFts-reload",{{"sid":"{RAPID_SID}","uid":"",'
                f'"provider":"{provider}","route":"{route}"}}]')
    time.sleep(1.5)
    poll_url = f"{base}&t={int(time.time()*1000)}"
    poll_text = http_get(poll_url, timeout=20)[0].decode("utf-8", "replace")
    payload = None
    for frame in poll_text.split("\x1e"):
        fm = re.match(r'^42\["onFts-client","(.*)"\]$', frame, re.S)
        if fm:
            payload = fm.group(1)
            break
    if not payload:
        raise ToolError("DATA_UNAVAILABLE",
                        "myrapidbus kiosk feed returned no vehicle frame",
                        retryable=True, retry_after_seconds=15,
                        details={"raw": poll_text[:80]})
    import base64
    import gzip as _gzip
    try:
        jdata = json.loads(_gzip.decompress(base64.b64decode(payload)).decode("utf-8"))
    except Exception as e:
        raise ToolError("DATA_UNAVAILABLE",
                        f"could not decode the kiosk vehicle frame: {e}",
                        retryable=True, retry_after_seconds=15)
    buses = jdata if isinstance(jdata, list) else []
    return {
        "provider": provider,
        "route": route or "all",
        "live_buses": len(buses),
        "returned": min(len(buses), limit),
        "truncated": len(buses) > limit,
        "buses": [
            {
                "bus_no": b.get("bus_no"), "latitude": b.get("latitude"),
                "longitude": b.get("longitude"), "route": b.get("route"),
                "dir": b.get("dir"), "speed": b.get("speed"),
                "angle": b.get("angle"), "dt_gps": b.get("dt_gps"),
                "trip_no": b.get("trip_no"), "accessibility": b.get("accessibility"),
            }
            for b in buses[:limit]
        ],
    }


# ---- MCP protocol (stdio JSON-RPC 2.0) ----
def get_flood_risk():
    """Live flood risk from JPS telemetry, via the dashboard's /api/flood proxy.

    The proxy fetches JPS's ~1.3 MB gauge feed server-side, keeps only
    danger/warning/alert stations with a reading in the last 24h (dead gauges
    excluded), and slims each station to name/coords/level/trend/timestamp.
    """
    data = http_get_json(f"{DASH}/api/flood?cb={int(time.time())}")
    return {
        "updated": data.get("updated"),
        "at_risk": data.get("at_risk"),
        "states": data.get("states", []),
        "stations": data.get("stations", []),
    }


def get_pricecatcher(item="", group="", limit=20):
    """PriceCatcher grocery price index (KPDN, 198-item basket, daily)."""
    data = http_get_json(f"{DASH}/prices.json")
    q = str(item or "").strip().lower()
    grp = str(group or "").strip().upper()
    lim = limit
    items = data.get("items") or []
    if q:
        items = [it for it in items if q in str(it.get("n", "")).lower()]
    if grp:
        items = [it for it in items if str(it.get("g", "")) == grp]
    matched = len(items)
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
        "as_of": data.get("asOf"),
        "generated": data.get("generated"),
        "months": data.get("months"),
        "currency": "MYR",
        "matched": matched,
        "returned": len(out),
        "truncated": matched > len(out),
        "basket": {"n": basket.get("n"), "base": basket.get("base"),
                   "national_index": basket.get("national")} if basket else None,
        "items": out,
    }


def get_tourism(country="", limit=10):
    """Monthly visitor arrivals (Tourism Malaysia, top 51, ~1 month lag)."""
    data = http_get_json(f"{DASH}/tourism.json")
    q = str(country or "").strip().lower()
    lim = limit
    rows = data.get("visitor") or []
    if q:
        rows = [r for r in rows if q in str(r.get("country", "")).lower()]
    matched = len(rows)
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
        "unit": "arrivals (persons)",
        "matched": matched, "returned": len(out), "truncated": matched > len(out),
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
    data = http_get_json(f"{DASH}/rapid_alerts.json")
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
    data = http_get_json(f"{DASH}/api/aqi?cb={int(time.time())}")
    stations = data.get("stations") or []
    return {
        "updated": data.get("updated"),
        "aqi_scale": "US AQI",
        "reading_time": data.get("reading_time"),
        "worst": data.get("worst"),
        "cleanest": data.get("cleanest"),
        "stations": stations,
    }


def get_hotel_performance(state=""):
    """Quarterly hotel performance by state (Tourism Malaysia Paid
    Accommodation Survey, via the dashboard's hotel.json).

    Occupancy rate (AOR), average room rate (ARR) and hotel guests
    (domestic/international) for all 16 states, current quarter vs a year
    earlier. Only the latest quarter is public on the source portal, so the
    dashboard collector probes newest-first; this returns the current quarter.
    """
    data = http_get_json(f"{DASH}/hotel.json")
    out = {"asOf": data.get("asOf"), "generated": data.get("generated"),
           "source": data.get("source"),
           "units": {"occupancy_rate": "percent",
                     "average_room_rate": "MYR per room-night",
                     "guests": "persons"}}
    if state:
        state = state.strip().title()
    for key, label in (("aor", "occupancy_rate"), ("arr", "average_room_rate"),
                       ("guests", "guests")):
        rows = data.get(key) or []
        if state:
            rows = [x for x in rows if str(x.get("state", "")).strip().title() == state]
        out[label] = rows
    return out


def get_election_results(category="", state="", query="", limit=50):
    """Latest election results from SPR (Suruhanjaya Pilihan Raya), via the
    dashboard's election.json.

    Categories: pru (PRU-15 parliamentary, 208 seats), dun (latest state
    election for every state - 600 seats across all 13 states) or prk (latest
    by-election). Optional state filter (e.g. 'KEDAH') and free-text query
    matched against constituency, winner or party name. Results are static
    once published - this is a one-time crawl per election.
    """
    data = http_get_json(f"{DASH}/election.json")
    seats = data.get("seats") or []
    if category:
        category = category.strip().lower()
        if category not in ("pru", "dun", "prk"):
            raise invalid("category must be one of pru, dun, prk",
                          category=category, allowed=["pru", "dun", "prk"])
        seats = [s for s in seats if s.get("category") == category]
    if state:
        state = state.strip().upper()
        seats = [s for s in seats if str(s.get("state", "")).upper() == state]
    if query:
        q = query.strip().lower()
        if q:
            def _matches(s):
                w = next((c for c in (s.get("candidates") or []) if c.get("isWinner")), None)
                hay = " ".join(str(x) for x in
                               [s.get("name"), s.get("state"), s.get("election"),
                                w.get("name") if w else "",
                                (w.get("partyShort") or w.get("party")) if w else ""]).lower()
                return q in hay
            seats = [s for s in seats if _matches(s)]
    # compact per-seat view - candidates are the heavy part
    def _compact(s):
        w = next((c for c in (s.get("candidates") or []) if c.get("isWinner")), None)
        return {
            "category": s.get("category"), "state": s.get("state"),
            "name": s.get("name"), "election": s.get("election"),
            "date": s.get("date"),
            "winner": w.get("name") if w else None,
            "party": (w.get("partyShort") or w.get("party")) if w else None,
            "votes": w.get("votes") if w else None,
            "majority": s.get("majority"), "totalVotes": s.get("totalVotes"),
        }
    matched = len(seats)
    page = seats[:limit]
    return {"generated": data.get("generated"), "source": data.get("source"),
            "note": data.get("note"),
            "categories": {k: (v or {}).get("name")
                           for k, v in (data.get("categories") or {}).items()},
            "matched": matched, "returned": len(page),
            "truncated": matched > len(page),
            "seats": [_compact(s) for s in page]}


# Every tool is a read-only fetch from an external government/public API, so
# they share the same annotation set: safe to call, safe to repeat, but the
# world beyond this server is what answers (openWorldHint).
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def _limit(default, maximum, what):
    return {"type": "integer", "minimum": 1, "maximum": maximum,
            "default": default,
            "description": f"Max {what} to return (default {default}, max {maximum})."}


TOOLS = [
    {
        "name": "mygov_weather_forecast",
        "description": "7-day weather forecast for Malaysian locations (MET Malaysia). "
                       "Returns date, morning/afternoon/night forecast and min/max "
                       "temperature in degrees Celsius.\n\n"
                       "Examples:\n"
                       "- location='Kota Bharu'\n"
                       "- location='Langkawi', limit=14\n"
                       "- no arguments -> a cross-section of locations nationwide",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string", "minLength": 2, "maxLength": 64,
                    "description": "Location name, case-insensitive partial match "
                                   "(e.g. 'Kota Bharu', 'Langkawi', 'Kuala Lumpur'). "
                                   "Omit for all locations.",
                },
                "limit": _limit(50, 200, "forecast records"),
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_weather_warning",
        "description": "Active weather warnings for Malaysia (MET Malaysia): heavy rain, "
                       "strong wind and rough sea warnings currently in force, with the "
                       "affected states and validity period. Takes no arguments.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_data_catalogue",
        "description": "Query the data.gov.my Data Catalogue (general government "
                       "datasets). Filters use data.gov.my's value@column syntax.\n\n"
                       "Examples:\n"
                       "- dataset_id='fuelprice', sort='-date', limit=5 "
                       "-> latest weekly RON95/RON97/diesel prices\n"
                       "- dataset_id='fuelprice', filter='level@series_type'\n"
                       "- dataset_id='fuelprice', date_start='2026-01-01@date'\n\n"
                       "Dataset ids come from https://data.gov.my/data-catalogue; "
                       "an unknown id returns NOT_FOUND.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset_id": {
                    "type": "string", "pattern": "^[a-z0-9_-]{2,64}$",
                    "description": "Dataset id, e.g. 'fuelprice'.",
                },
                "limit": _limit(50, 500, "records"),
                "filter": {"type": "string",
                           "description": "Exact match as value@column, e.g. 'level@series_type'."},
                "contains": {"type": "string",
                             "description": "Partial match as value@column."},
                "sort": {"type": "string",
                         "description": "Column name, or -column for descending, e.g. '-date'."},
                "date_start": {"type": "string",
                               "description": "Inclusive start as YYYY-MM-DD@column, e.g. '2026-01-01@date'."},
                "date_end": {"type": "string",
                             "description": "Inclusive end as YYYY-MM-DD@column."},
            },
            "required": ["dataset_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_opendosm",
        "description": "Query OpenDOSM (Department of Statistics Malaysia economic and "
                       "social statistics). Same value@column filter syntax as the data "
                       "catalogue.\n\n"
                       "Examples:\n"
                       "- dataset_id='cpi_core', sort='-date', limit=12 "
                       "-> last 12 months of the core CPI index\n"
                       "- dataset_id='cpi_core', date_start='2025-01-01@date'\n\n"
                       "Dataset ids come from https://open.dosm.gov.my/data-catalogue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset_id": {
                    "type": "string", "pattern": "^[a-z0-9_-]{2,64}$",
                    "description": "Dataset id, e.g. 'cpi_core'.",
                },
                "limit": _limit(50, 500, "records"),
                "filter": {"type": "string",
                           "description": "Exact match as value@column."},
                "sort": {"type": "string",
                         "description": "Column name, or -column for descending, e.g. '-date'."},
                "date_start": {"type": "string",
                               "description": "Inclusive start as YYYY-MM-DD@column."},
                "date_end": {"type": "string",
                             "description": "Inclusive end as YYYY-MM-DD@column."},
            },
            "required": ["dataset_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_gtfs_static_summary",
        "description": "Summarize an agency's GTFS static schedule feed: file list, row "
                       "counts for routes/stops/trips, and a few sample routes. Use this "
                       "to discover what a network publishes before querying live "
                       "positions.\n\n"
                       "Examples:\n"
                       "- agency='ktmb' -> KTM Berhad intercity/Komuter schedule\n"
                       "- agency='prasarana' -> Rapid KL bus schedule",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agency": {
                    "type": "string",
                    "enum": ["ktmb", "prasarana", "mybas-johor-bahru",
                             "mybas-kota-bharu", "mybas-alor-setar",
                             "mybas-kuala-terengganu"],
                    "default": "ktmb",
                    "description": "Transit agency feed to summarize.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_gtfs_realtime",
        "description": "Live vehicle positions from the data.gov.my GTFS-realtime feed. "
                       "Returns the live vehicle count plus id/lat/lon/bearing/speed for "
                       "up to `limit` vehicles.\n\n"
                       "Examples:\n"
                       "- agency='ktmb' -> live KTM trains\n"
                       "- agency='prasarana', category='rapid-rail-kl' -> LRT/MRT trains\n\n"
                       "NOTE: the prasarana rapid-bus-kl feed here is frequently empty — "
                       "use mygov_rapid_bus_live for actual Rapid bus positions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agency": {
                    "type": "string",
                    "enum": ["ktmb", "prasarana", "mybas-johor-bahru"],
                    "default": "ktmb",
                    "description": "Transit agency.",
                },
                "category": {
                    "type": "string",
                    "enum": ["rapid-bus-kl", "rapid-rail-kl", "rapid-bus-penang",
                             "rapid-bus-kuantan", "rapid-bus-mrtfeeder"],
                    "description": "Required for agency='prasarana'; ignored otherwise.",
                },
                "limit": _limit(50, 500, "vehicles"),
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_rapid_bus_live",
        "description": "Live Rapid bus positions from the official myrapidbus kiosk AVL "
                       "feed (800+ buses in the Klang Valley alone). Returns bus_no, "
                       "latitude/longitude, route, direction, speed and last GPS time.\n\n"
                       "Examples:\n"
                       "- provider='RKL', route='T200' -> just that route's buses\n"
                       "- provider='RPG' -> Rapid Penang, first 50 buses\n\n"
                       "Always pass `route` when asking about a specific service — the "
                       "unfiltered fleet is large and gets truncated to `limit`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string", "enum": ["RKL", "RPG", "RKN"], "default": "RKL",
                    "description": "RKL = Rapid KL (Klang Valley), RPG = Rapid Penang, "
                                   "RKN = Rapid Kuantan.",
                },
                "route": {
                    "type": "string", "pattern": "^[A-Za-z0-9-]{1,16}$",
                    "description": "Route number filter, e.g. 'T200', '300'. Omit for the whole fleet.",
                },
                "limit": _limit(50, 500, "buses"),
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_flood_risk",
        "description": "Live flood risk from JPS (Department of Irrigation and Drainage) "
                       "water-level telemetry. Returns every station currently at "
                       "danger/warning/alert — name, state, district, latitude/longitude, "
                       "water level and danger threshold in metres, trend and last reading "
                       "time — plus a per-state count. Gauges that have not reported in 24h "
                       "are excluded. Takes no arguments.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_pricecatcher",
        "description": "Malaysian grocery prices from the KPDN PriceCatcher 198-item "
                       "basket. Returns each item's latest price in MYR, its unit, "
                       "month-on-month and year-on-year change, and a 13-month price "
                       "history.\n\n"
                       "Examples:\n"
                       "- item='TOMATO'\n"
                       "- item='BERAS' (rice)\n"
                       "- group='BARANGAN SEGAR', limit=30 (fresh produce)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string", "minLength": 2, "maxLength": 64,
                    "description": "Item name substring, case-insensitive (e.g. 'TOMATO', 'AYAM', 'BERAS').",
                },
                "group": {
                    "type": "string",
                    "enum": ["BARANGAN SEGAR", "BARANGAN BERBUNGKUS",
                             "MAKANAN KERING", "MINUMAN", "BARANGAN LAIN"],
                    "description": "Item group filter.",
                },
                "limit": _limit(20, 200, "items"),
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_tourism_arrivals",
        "description": "Malaysian monthly international visitor arrivals by country of "
                       "nationality (Tourism Malaysia, top 51 markets). Returns the "
                       "month's arrivals, month-on-month and year-on-year growth, "
                       "recovery vs pre-pandemic 2019, and the year-to-date picture. "
                       "Published monthly with roughly a one-month lag — check "
                       "meta.data_period before describing it as current.\n\n"
                       "Examples:\n"
                       "- country='SINGAPORE'\n"
                       "- limit=10 -> the ten largest source markets",
        "inputSchema": {
            "type": "object",
            "properties": {
                "country": {
                    "type": "string", "minLength": 2, "maxLength": 64,
                    "description": "Country/nationality substring, case-insensitive "
                                   "(e.g. 'SINGAPORE', 'CHINA', 'INDIA').",
                },
                "limit": _limit(10, 60, "countries"),
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_rapid_service_alert",
        "description": "Latest Rapid KL service alert (LRT/MRT/monorail/bus disruption) "
                       "from myrapid.com.my PULSE. Returns the newest post only: title, "
                       "excerpt, link and posted time. Refreshed about every 10 minutes. "
                       "Takes no arguments.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_air_quality",
        "description": "Live air quality for 18 major Malaysian cities on the US AQI "
                       "scale, with PM2.5 in micrograms per cubic metre. Stations are "
                       "sorted worst-first and the cleanest is included for comparison. "
                       "US AQI 101+ (Unhealthy) is the haze alert threshold. Takes no "
                       "arguments.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_hotel_performance",
        "description": "Quarterly hotel performance by state from Tourism Malaysia's Paid "
                       "Accommodation Survey: occupancy rate (percent), average room rate "
                       "(MYR per room-night) and hotel guests (domestic/international) for "
                       "all 16 states, current quarter vs a year earlier. Only the latest "
                       "quarter is published — see meta.data_period.\n\n"
                       "Examples:\n"
                       "- state='Pahang'\n"
                       "- no arguments -> all states",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string", "minLength": 3, "maxLength": 32,
                    "description": "State name, e.g. 'Pahang', 'Kuala Lumpur', 'Sabah'. "
                                   "Omit for all 16 states.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "mygov_election_results",
        "description": "Election results from SPR (Suruhanjaya Pilihan Raya): PRU-15 "
                       "parliamentary seats, the latest state election for every state, "
                       "or the latest by-election. Returns constituency, winner, party, "
                       "votes and majority. Results are static once published.\n\n"
                       "Examples:\n"
                       "- category='pru', state='KEDAH'\n"
                       "- query='Anwar' -> seats won by a matching candidate\n"
                       "- category='prk' -> the most recent by-election",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string", "enum": ["pru", "dun", "prk"],
                    "description": "pru = parliamentary (208 seats), dun = state assembly "
                                   "(600 seats across 13 states), prk = by-election.",
                },
                "state": {
                    "type": "string", "minLength": 3, "maxLength": 32,
                    "description": "State name, e.g. 'KEDAH' (matched case-insensitively).",
                },
                "query": {
                    "type": "string", "minLength": 2, "maxLength": 64,
                    "description": "Free text matched against constituency, winner or party name.",
                },
                "limit": _limit(50, 800, "seats"),
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
]


def rpc_response(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def _schema(tool, prop):
    return TOOLS_BY_NAME[tool]["inputSchema"]["properties"].get(prop, {})


def limit_arg(tool, args):
    """Resolve `limit` against the tool's own schema.

    The schema advertises the bounds so the model asks for something sensible;
    this clamp enforces them, because a schema is guidance and a client may
    ignore it entirely.
    """
    spec = _schema(tool, "limit")
    default = spec.get("default", 50)
    lo, hi = spec.get("minimum", 1), spec.get("maximum", 500)
    raw = args.get("limit")
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise invalid("limit must be an integer", limit=raw,
                      minimum=lo, maximum=hi, default=default)
    return max(lo, min(n, hi))


def enum_arg(tool, prop, args, default=None, upper=False):
    """Validate an enum-constrained string, echoing the allowed values back."""
    raw = args.get(prop)
    if raw is None or raw == "":
        return default
    val = str(raw).strip()
    if upper:
        val = val.upper()
    allowed = _schema(tool, prop).get("enum")
    if allowed and val not in allowed:
        raise invalid(f"{prop} must be one of: {', '.join(allowed)}",
                      **{prop: raw, "allowed": allowed})
    return val


def str_arg(args, prop, default=""):
    raw = args.get(prop)
    return default if raw is None else str(raw).strip()


def call_tool(name, args):
    a = args or {}
    if name not in TOOLS_BY_NAME:
        raise ToolError("NOT_FOUND", f"unknown tool: {name}",
                        details={"available": sorted(TOOLS_BY_NAME)})

    if name == "mygov_weather_forecast":
        data = get_weather_forecast(str_arg(a, "location") or None,
                                    limit_arg(name, a))
        return envelope(name, data)

    if name == "mygov_weather_warning":
        return envelope(name, get_weather_warning())

    if name in ("mygov_data_catalogue", "mygov_opendosm"):
        dataset_id = str_arg(a, "dataset_id")
        if not re.match(r"^[a-z0-9_-]{2,64}$", dataset_id):
            raise invalid("dataset_id is required and must be a dataset slug "
                          "(lowercase letters, digits, _ or -)",
                          dataset_id=a.get("dataset_id"))
        keys = ("filter", "contains", "sort", "date_start", "date_end")
        filters = {k: v for k, v in a.items() if k in keys and v}
        fetch = (get_data_catalogue if name == "mygov_data_catalogue"
                 else get_opendosm)
        data = fetch(dataset_id, limit_arg(name, a), filters)
        return envelope(name, data, dataset=dataset_id)

    if name == "mygov_gtfs_static_summary":
        agency = enum_arg(name, "agency", a, default="ktmb")
        return envelope(name, get_gtfs_static_summary(agency), dataset=agency)

    if name == "mygov_gtfs_realtime":
        agency = enum_arg(name, "agency", a, default="ktmb")
        category = enum_arg(name, "category", a)
        if agency == "prasarana" and not category:
            raise invalid("agency='prasarana' needs a category "
                          "(e.g. rapid-rail-kl)",
                          allowed=_schema(name, "category")["enum"])
        data = get_gtfs_realtime(agency, category, limit_arg(name, a))
        return envelope(name, data)

    if name == "mygov_rapid_bus_live":
        provider = enum_arg(name, "provider", a, default="RKL", upper=True)
        route = str_arg(a, "route")
        if route and not re.match(r"^[A-Za-z0-9-]{1,16}$", route):
            raise invalid("route must be 1-16 letters, digits or hyphens "
                          "(e.g. 'T200', '300')", route=route)
        data = get_rapid_bus_live(provider, route, limit_arg(name, a))
        return envelope(name, data)

    if name == "mygov_flood_risk":
        data = get_flood_risk()
        return envelope(name, data, data_updated_at=data.get("updated"))

    if name == "mygov_pricecatcher":
        data = get_pricecatcher(str_arg(a, "item"),
                                enum_arg(name, "group", a, default="") or "",
                                limit_arg(name, a))
        return envelope(name, data, data_period=data.get("as_of"),
                        data_updated_at=data.get("generated"))

    if name == "mygov_tourism_arrivals":
        data = get_tourism(str_arg(a, "country"), limit_arg(name, a))
        return envelope(name, data, data_period=data.get("as_of"),
                        data_updated_at=data.get("generated"))

    if name == "mygov_rapid_service_alert":
        data = get_rapid_service_alert()
        return envelope(name, data, data_updated_at=data.get("updated"))

    if name == "mygov_air_quality":
        data = get_air_quality()
        return envelope(name, data, data_period=data.get("reading_time"),
                        data_updated_at=data.get("updated"))

    if name == "mygov_hotel_performance":
        data = get_hotel_performance(str_arg(a, "state"))
        return envelope(name, data, data_period=data.get("asOf"),
                        data_updated_at=data.get("generated"))

    if name == "mygov_election_results":
        data = get_election_results(str_arg(a, "category"), str_arg(a, "state"),
                                    str_arg(a, "query"), limit_arg(name, a))
        return envelope(name, data, data_updated_at=data.get("generated"))

    raise ToolError("INTERNAL_ERROR", f"tool {name} is declared but not wired up")


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
                payload, is_error = result, False
            except ToolError as e:
                payload, is_error = e.to_dict(), True
            except Exception as e:  # nothing should reach here; report it cleanly
                payload = ToolError("INTERNAL_ERROR",
                                    f"{type(e).__name__}: {e}").to_dict()
                is_error = True
            # Tool failures come back as an isError result rather than a
            # JSON-RPC error so the calling model can read the code and decide
            # whether to retry, fix its arguments, or give up.
            content = [{"type": "text",
                        "text": json.dumps(payload, ensure_ascii=False, default=str)}]
            sys.stdout.write(json.dumps(rpc_response(mid, {
                "content": content,
                "isError": is_error,
            })) + "\n")
            sys.stdout.flush()
        elif method == "ping":
            sys.stdout.write(json.dumps(rpc_response(mid, {})) + "\n")
            sys.stdout.flush()
        elif mid is not None:
            # Unknown request (as opposed to a notification): answer with the
            # standard JSON-RPC code instead of leaving the client hanging.
            sys.stdout.write(json.dumps(rpc_error(
                mid, -32601, f"method not found: {method}")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
