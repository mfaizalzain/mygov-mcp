#!/usr/bin/env python3
"""MCP server exposing Malaysia Government Open API (api.data.gov.my) as tools.

Dependency-free: speaks the MCP stdio JSON-RPC protocol directly (initialize,
tools/list, tools/call) using only the Python standard library. Register with
Claude Code via:  claude mcp add -s user mygov -- python3 <this file>

API reference: https://developer.data.gov.my  (base: https://api.data.gov.my)
Rate limit: 4 req/min per API family — the server keeps a per-family throttle.
"""
import base64
import binascii
import hashlib
import json
import re
import socket
import sys
import threading
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
SERVER_VERSION = "1.2.0"
UA = f"mygov-mcp/{SERVER_VERSION} (+https://malaysia-at-a-glance.com)"


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


# ---- response cache ----
# Caching sits at the HTTP layer, keyed by URL, so every tool that reads the
# same upstream file shares one entry and client-side filtering/paging never
# re-fetches. It protects the upstream government services as much as us.
class TTLCache:
    def __init__(self, max_entries=128):
        self._lock = threading.Lock()
        self._store = {}
        self._max = max_entries

    def get(self, key):
        """Return (value, age_seconds) if a live entry exists, else None."""
        now = time.time()
        with self._lock:
            hit = self._store.get(key)
            if not hit:
                return None
            value, stored_at, ttl = hit
            if now - stored_at >= ttl:
                del self._store[key]
                return None
            return value, int(now - stored_at)

    def set(self, key, value, ttl):
        with self._lock:
            if len(self._store) >= self._max:
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (value, time.time(), ttl)

    def stats(self):
        now = time.time()
        with self._lock:
            return {"entries": len(self._store), "max_entries": self._max,
                    "live": sum(1 for v in self._store.values()
                                if now - v[1] < v[2])}


CACHE = TTLCache()

# How long each upstream stays fresh. Live vehicle feeds move constantly;
# a quarterly survey does not.
TTL = {
    "rapid_bus": 90, "gtfs_realtime": 20, "flood": 120, "aqi": 600,
    "weather": 600, "weather_warning": 300, "rapid_alert": 300,
    "prices": 3600, "catalogue": 900, "tourism": 86400, "hotel": 86400,
    "election": 86400, "gtfs_static": 86400,
}

# Per-tool-call record of whether the data came from cache, so meta.cache can
# tell a client it is looking at a response that is a few seconds old.
_TRACE = threading.local()


def _trace_reset():
    _TRACE.entries = []


def _trace_add(status, age, ttl):
    if getattr(_TRACE, "entries", None) is None:
        _TRACE.entries = []
    _TRACE.entries.append((status, age, ttl))


def _trace_summary():
    entries = getattr(_TRACE, "entries", None) or []
    if not entries:
        return None
    age = max(e[1] for e in entries)
    return {"status": "hit" if all(e[0] == "hit" for e in entries) else "miss",
            "age_seconds": age, "ttl_seconds": min(e[2] for e in entries)}


def cache_bucket(ttl):
    """A cache-buster that only changes once per TTL window.

    Some upstreams sit behind a CDN and need a changing query param, but a
    per-request timestamp would make every response uncacheable for us too.
    """
    return int(time.time() // max(ttl, 1))


def http_get(url, headers=None, timeout=30, ttl=0):
    """GET returning raw bytes, with upstream failures mapped to ToolError."""
    if ttl:
        cached = CACHE.get(url)
        if cached is not None:
            value, age = cached
            _trace_add("hit", age, ttl)
            return value
        _trace_add("miss", 0, ttl)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body, hdrs = r.read(), dict(r.headers)
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise _upstream_error(e, url)
    if ttl:
        CACHE.set(url, (body, hdrs), ttl)
    return body, hdrs


def http_get_json(url, headers=None, timeout=30, ttl=0):
    body, _ = http_get(url, headers, timeout, ttl)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise ToolError("DATA_UNAVAILABLE",
                        f"upstream returned a non-JSON body: {e}",
                        retryable=True, retry_after_seconds=30,
                        details={"url": url})


# ---- cursor pagination ----
# Cursors are opaque to the client but carry a fingerprint of the query, so a
# cursor from a different search can be rejected instead of silently paging
# through the wrong result set.
def _fingerprint(parts):
    raw = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


def encode_cursor(offset, fingerprint):
    raw = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor, fingerprint):
    pad = "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + pad))
        offset = int(payload["o"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise invalid("cursor is not a cursor issued by this server; omit it "
                      "to start from the beginning", cursor=cursor)
    if payload.get("f") != fingerprint:
        raise invalid("cursor belongs to a different query — reissue it by "
                      "repeating the request without a cursor", cursor=cursor)
    return max(offset, 0)


def paginate(items, limit, cursor, query):
    """Slice `items` into a page and describe the rest.

    `query` is whatever identifies this result set (the filter arguments);
    it is fingerprinted into the cursor.
    """
    fp = _fingerprint(query)
    offset = decode_cursor(cursor, fp) if cursor else 0
    page = items[offset:offset + limit]
    nxt = offset + len(page)
    has_more = nxt < len(items)
    return page, {
        "total": len(items),
        "returned": len(page),
        "offset": offset,
        "has_more": has_more,
        "next_cursor": encode_cursor(nxt, fp) if has_more else None,
    }


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
    "mygov_dataset_info": {
        "source": "data.gov.my / OpenDOSM catalogue metadata",
        "source_url": "https://developer.data.gov.my",
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
    cache = _trace_summary()
    if cache:
        meta["cache"] = cache
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
    ent = {"id": None, "latitude": None, "longitude": None, "timestamp": None}
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
                p["latitude"] = round(val, 6)
            elif field == 2:
                p["longitude"] = round(val, 6)
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


def api_get(path, params=None, ttl=0, family=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    # Only spend rate-limit budget (and only sleep) when we are actually going
    # to the network — a cache hit must not block.
    if family and not (ttl and CACHE.get(url)):
        THROTTLE.wait(family)
    body, headers = http_get(url, headers={"User-Agent": "Mozilla/5.0 mygov-mcp"},
                             ttl=ttl)
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
    params = {"limit": limit}
    if location:
        params["contains"] = f"{location}@location__location_name"
    return api_get("/weather/forecast", params, ttl=TTL["weather"],
                   family="weather")


def get_weather_warning():
    return api_get("/weather/warning", ttl=TTL["weather_warning"],
                   family="weather")


def get_data_catalogue(dataset_id, limit=100, filters=None, meta=True):
    params = {"id": dataset_id, "limit": limit}
    if meta:
        params["meta"] = "true"
    if filters:
        params.update(filters)
    return api_get("/data-catalogue", params, ttl=TTL["catalogue"],
                   family="data-catalogue")


def get_opendosm(dataset_id, limit=100, filters=None, meta=True):
    params = {"id": dataset_id, "limit": limit}
    if meta:
        params["meta"] = "true"
    if filters:
        params.update(filters)
    return api_get("/opendosm", params, ttl=TTL["catalogue"],
                   family="opendosm")


def get_gtfs_static_summary(agency):
    """Download GTFS static ZIP and summarize routes/stops/trips."""
    if not re.match(r"^[a-z0-9-]{1,32}$", agency):
        raise invalid("agency must be lowercase letters, digits or hyphens",
                      agency=agency)
    suffix = "?category=rapid-bus-kl" if agency.startswith("prasarana") else ""
    data = api_get(f"/gtfs-static/{agency}{suffix}", ttl=TTL["gtfs_static"],
                   family="gtfs")
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
    if not re.match(r"^[a-z0-9-]{1,32}$", agency):
        raise invalid("agency must be lowercase letters, digits or hyphens",
                      agency=agency)
    if category and not re.match(r"^[a-z0-9-]{1,32}$", category):
        raise invalid("category must be lowercase letters, digits or hyphens",
                      category=category)
    path = f"/gtfs-realtime/vehicle-position/{agency}"
    if category:
        path += f"?category={category}"
    data = api_get(path, ttl=TTL["gtfs_realtime"], family="gtfs")
    return _parse_feed_message(data)


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


def _fetch_rapid_fleet(provider):
    """One full socket.io round-trip for a provider's whole fleet.

    Fetching the fleet unfiltered (rather than per route) means one upstream
    hit serves every route query while the entry is warm.
    """
    handshake_url = f"{RAPID_URL}?EIO=4&transport=polling&t={int(time.time()*1000)}"
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
                f'"provider":"{provider}","route":""}}]')
    # The kiosk server needs a moment to push the first frame. This sleep is
    # why the collector thread exists: it is paid off-request once per TTL
    # window, not by every caller.
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
    import gzip as _gzip
    try:
        jdata = json.loads(_gzip.decompress(base64.b64decode(payload)).decode("utf-8"))
    except Exception as e:
        raise ToolError("DATA_UNAVAILABLE",
                        f"could not decode the kiosk vehicle frame: {e}",
                        retryable=True, retry_after_seconds=15)
    return jdata if isinstance(jdata, list) else []


class RapidCollector:
    """Keeps recently-requested Rapid fleets warm in the background.

    A request used to pay a full handshake plus a 1.5s wait inline. Now the
    first request for a provider pays that once, and a daemon thread refreshes
    it every TTL window so later requests are served from cache immediately.
    The thread only refreshes providers asked for in the last IDLE_AFTER
    seconds, then exits — an idle server makes no upstream traffic.

    REFRESH is deliberately shorter than the cache TTL: a round-trip takes
    several seconds, so refreshing on the TTL boundary would let the entry
    expire mid-fetch and drop the next caller back onto the slow path. The
    TTL is the staleness bound (how old data may get if refreshes fail);
    REFRESH is how often it is actually renewed.
    """
    IDLE_AFTER = 300
    REFRESH = 20

    def __init__(self):
        self._lock = threading.Lock()
        self._demand = {}
        self._thread = None

    def _key(self, provider):
        return f"rapid-fleet:{provider}"

    def fleet(self, provider):
        with self._lock:
            self._demand[provider] = time.time()
        key = self._key(provider)
        cached = CACHE.get(key)
        if cached is not None:
            buses, age = cached
            _trace_add("hit", age, TTL["rapid_bus"])
            self._ensure_thread()
            return buses
        _trace_add("miss", 0, TTL["rapid_bus"])
        buses = _fetch_rapid_fleet(provider)
        CACHE.set(key, buses, TTL["rapid_bus"])
        self._ensure_thread()
        return buses

    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="rapid-collector")
            self._thread.start()

    def _wanted(self):
        cutoff = time.time() - self.IDLE_AFTER
        with self._lock:
            self._demand = {p: t for p, t in self._demand.items() if t > cutoff}
            return list(self._demand)

    def _run(self):
        while True:
            time.sleep(self.REFRESH)
            providers = self._wanted()
            if not providers:
                return
            for provider in providers:
                try:
                    CACHE.set(self._key(provider),
                              _fetch_rapid_fleet(provider), TTL["rapid_bus"])
                except ToolError:
                    # A refresh failure just leaves the previous entry to
                    # expire; the next request will surface the real error.
                    pass

    def status(self):
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            return {"running": running, "providers": sorted(self._demand)}


RAPID = RapidCollector()


def get_rapid_bus_live(provider="RKL", route=""):
    """Whole-fleet snapshot for a provider, route-filtered locally.

    Returns (buses, context); context explains an empty route filter rather
    than leaving the caller with a bare zero.
    """
    fleet = RAPID.fleet(provider)
    buses = fleet
    if route:
        want = route.upper()
        exact = [b for b in buses if str(b.get("route", "")).upper() == want]
        # Kiosk route codes are not always what riders say ("T200" is served by
        # route "T2000"), so fall back to a prefix match rather than nothing.
        buses = exact or [b for b in buses
                          if str(b.get("route", "")).upper().startswith(want)]
    context = {}
    if route and not buses:
        # Kiosk codes are 5-character ("U6000"), not the rider-facing numbers,
        # so an empty result usually means the wrong code, not an idle route.
        codes = sorted({str(b.get("route")) for b in fleet if b.get("route")})
        context = {
            "note": f"No bus is reporting on route '{route}'. Route codes in "
                    f"this feed are the operator's own (e.g. 'U6000', "
                    f"'T2000'), which may differ from the number on the bus.",
            "known_route_count": len(codes),
            "known_routes_sample": codes[:40],
        }
    rows = [
        {
            "bus_no": b.get("bus_no"), "latitude": b.get("latitude"),
            "longitude": b.get("longitude"), "route": b.get("route"),
            "dir": b.get("dir"), "speed": b.get("speed"),
            "speed_unit": "km/h", "angle": b.get("angle"),
            "dt_gps": b.get("dt_gps"), "trip_no": b.get("trip_no"),
            "accessibility": b.get("accessibility"),
        }
        for b in buses
    ]
    return rows, context


# ---- MCP protocol (stdio JSON-RPC 2.0) ----
def get_flood_risk():
    """Live flood risk from JPS telemetry, via the dashboard's /api/flood proxy.

    The proxy fetches JPS's ~1.3 MB gauge feed server-side, keeps only
    danger/warning/alert stations with a reading in the last 24h (dead gauges
    excluded), and slims each station to name/coords/level/trend/timestamp.
    """
    data = http_get_json(f"{DASH}/api/flood?cb={cache_bucket(TTL['flood'])}",
                         ttl=TTL["flood"])
    # JPS publishes lat/lon; every tool here reports latitude/longitude so a
    # client never has to guess which spelling a given feed used.
    stations = []
    for s in data.get("stations", []):
        st = dict(s)
        st["latitude"] = st.pop("lat", None)
        st["longitude"] = st.pop("lon", None)
        stations.append(st)
    return {
        "updated": data.get("updated"),
        "at_risk": data.get("at_risk"),
        "units": {"level": "metres", "dangerLevel": "metres",
                  "margin": "metres"},
        "states": data.get("states", []),
        "stations": stations,
    }


def get_pricecatcher(item="", group=""):
    """PriceCatcher grocery price index (KPDN, 198-item basket).

    Returns every matching item; the caller pages the list.
    """
    data = http_get_json(f"{DASH}/prices.json", ttl=TTL["prices"])
    q = str(item or "").strip().lower()
    grp = str(group or "").strip().upper()
    months = data.get("months") or []
    items = data.get("items") or []
    if q:
        items = [it for it in items if q in str(it.get("n", "")).lower()]
    if grp:
        items = [it for it in items if str(it.get("g", "")) == grp]
    rows = []
    for it in items:
        p = it.get("p") or []
        rows.append({
            "item": it.get("n"), "unit": it.get("u"), "group": it.get("g"),
            "kind": it.get("k"),
            "latest_price": p[-1] if p else None,
            "mom_pct": it.get("mom"), "yoy_pct": it.get("yoy"),
            "price_history": [{"month": months[i], "price": v}
                              for i, v in enumerate(p) if i < len(months)],
        })
    basket = data.get("basket") or {}
    context = {
        "as_of": data.get("asOf"),
        "generated": data.get("generated"),
        "months": months,
        "currency": "MYR",
        "units": {"latest_price": "MYR per item unit",
                  "mom_pct": "percent", "yoy_pct": "percent"},
        "basket": {"n": basket.get("n"), "base": basket.get("base"),
                   "national_index": basket.get("national")} if basket else None,
    }
    return rows, context


def get_tourism(country=""):
    """Monthly visitor arrivals (Tourism Malaysia, top 51, ~1 month lag)."""
    data = http_get_json(f"{DASH}/tourism.json", ttl=TTL["tourism"])
    q = str(country or "").strip().lower()
    rows = data.get("visitor") or []
    if q:
        rows = [r for r in rows if q in str(r.get("country", "")).lower()]
    out = [{
        "rank": r.get("rank"), "country": r.get("country"),
        "arrivals": r.get("cur"), "prev_month": r.get("prev"),
        "yoy_pct": r.get("g_yoy"), "vs_2019_pct": r.get("g_2019"),
        "mom_pct": r.get("g_mom"), "ytd_arrivals": r.get("ytd26"),
        "ytd_yoy_pct": r.get("gy_yoy"),
    } for r in rows]
    context = {
        "as_of": data.get("asOf"), "generated": data.get("generated"),
        "totals": data.get("totals"),
        "units": {"arrivals": "persons", "ytd_arrivals": "persons",
                  "yoy_pct": "percent", "mom_pct": "percent",
                  "vs_2019_pct": "percent"},
    }
    return out, context


def get_rapid_service_alert():
    """Latest Rapid KL service alert (myrapid.com.my PULSE).

    The source is behind Incapsula (a JS-challenge WAF; its wp-json also
    returns 401 for anonymous reads), so the dashboard's collect_rapid
    workflow scrapes it via the r.jina.ai reader every 10 min and publishes
    the newest post as rapid_alerts.json. This tool returns that file - the
    same data the dashboard's alert deck shows: one card, latest post only.
    """
    data = http_get_json(f"{DASH}/rapid_alerts.json", ttl=TTL["rapid_alert"])
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
    data = http_get_json(f"{DASH}/api/aqi?cb={cache_bucket(TTL['aqi'])}",
                         ttl=TTL["aqi"])
    stations = []
    for s in data.get("stations") or []:
        st = dict(s)
        try:  # the model publishes pm2.5 as a string
            st["pm25"] = float(st["pm25"]) if st.get("pm25") is not None else None
        except (TypeError, ValueError):
            pass
        stations.append(st)
    return {
        "updated": data.get("updated"),
        "aqi_scale": "US AQI",
        "units": {"aqi": "US AQI index", "pm25": "µg/m³"},
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
    data = http_get_json(f"{DASH}/hotel.json", ttl=TTL["hotel"])
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


def get_election_results(category="", state="", query=""):
    """Latest election results from SPR (Suruhanjaya Pilihan Raya), via the
    dashboard's election.json.

    Categories: pru (PRU-15 parliamentary, 208 seats), dun (latest state
    election for every state - 600 seats across all 13 states) or prk (latest
    by-election). Optional state filter (e.g. 'KEDAH') and free-text query
    matched against constituency, winner or party name. Results are static
    once published - this is a one-time crawl per election.
    """
    data = http_get_json(f"{DASH}/election.json", ttl=TTL["election"])
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

    context = {
        "generated": data.get("generated"), "source": data.get("source"),
        "note": data.get("note"),
        "units": {"votes": "votes", "majority": "votes", "totalVotes": "votes"},
        "categories": {k: (v or {}).get("name")
                       for k, v in (data.get("categories") or {}).items()},
    }
    return [_compact(s) for s in seats], context


# Tools whose provenance is fixed, plus the two generic query tools whose
# metadata has to come from data.gov.my per dataset.
CATALOGUE_APIS = {"data-catalogue": "/data-catalogue", "opendosm": "/opendosm"}


def get_dataset_info(api, dataset_id):
    """Publisher metadata for one data.gov.my / OpenDOSM dataset.

    Answers "how recent is this?" without making a client infer it from the
    rows: who publishes it, when it was last updated, when the next update is
    due, and what the columns are.
    """
    path = CATALOGUE_APIS[api]
    payload = api_get(path, {"id": dataset_id, "limit": 1, "meta": "true",
                             "sort": "-date"},
                      ttl=TTL["catalogue"], family=api)
    if not isinstance(payload, dict) or "meta" not in payload:
        raise ToolError("NOT_FOUND",
                        f"{api} has no dataset with id '{dataset_id}'",
                        details={"dataset_id": dataset_id, "api": api})
    meta = payload.get("meta") or {}
    sample = (payload.get("data") or [{}])[0]
    return {
        "api": api,
        "dataset_id": meta.get("catalogue_id", dataset_id),
        "publisher": meta.get("data_source"),
        "data_as_of": meta.get("data_as_of"),
        "last_updated": meta.get("last_updated"),
        "next_update": meta.get("next_update"),
        "update_frequency": meta.get("update_frequency"),
        "columns": sorted(sample) if sample else [],
        "latest_row": sample or None,
        "source_url": f"{BASE}{path}?id={dataset_id}",
        "note": "Row counts are not reported here — this call fetches a single "
                "row on purpose. Query the dataset itself for volume.",
    }


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
            "description": f"Page size — max {what} per page "
                           f"(default {default}, max {maximum})."}


CURSOR = {
    "type": "string", "maxLength": 128,
    "description": "Opaque cursor from a previous response's next_cursor. "
                   "Omit for the first page; repeat the same filters when "
                   "passing one.",
}


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
        "name": "mygov_dataset_info",
        "description": "Metadata for one data.gov.my or OpenDOSM dataset: who "
                       "publishes it, what it is as of, when it was last "
                       "updated, when the next update is due, its update "
                       "frequency, its column names and the latest row.\n\n"
                       "Call this before mygov_data_catalogue / mygov_opendosm "
                       "when you need to know how current a dataset is, or "
                       "which columns you can filter and sort on.\n\n"
                       "Examples:\n"
                       "- api='data-catalogue', dataset_id='fuelprice'\n"
                       "- api='opendosm', dataset_id='cpi_core'",
        "inputSchema": {
            "type": "object",
            "properties": {
                "api": {
                    "type": "string", "enum": ["data-catalogue", "opendosm"],
                    "default": "data-catalogue",
                    "description": "Which API the dataset lives in. "
                                   "opendosm holds DOSM statistics; "
                                   "data-catalogue holds everything else.",
                },
                "dataset_id": {
                    "type": "string", "pattern": "^[a-z0-9_-]{2,64}$",
                    "description": "Dataset id, e.g. 'fuelprice' or 'cpi_core'.",
                },
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
                "cursor": CURSOR,
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
                "cursor": CURSOR,
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
                "cursor": CURSOR,
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
                "cursor": CURSOR,
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
                "cursor": CURSOR,
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


def dataset_id_arg(args):
    dataset_id = str_arg(args, "dataset_id")
    if not re.match(r"^[a-z0-9_-]{2,64}$", dataset_id):
        raise invalid("dataset_id is required and must be a dataset slug "
                      "(lowercase letters, digits, _ or -). Browse ids at "
                      "https://data.gov.my/data-catalogue",
                      dataset_id=args.get("dataset_id"))
    return dataset_id


def str_arg(args, prop, default=""):
    raw = args.get(prop)
    return default if raw is None else str(raw).strip()


def call_tool(name, args):
    a = args or {}
    if name not in TOOLS_BY_NAME:
        raise ToolError("NOT_FOUND", f"unknown tool: {name}",
                        details={"available": sorted(TOOLS_BY_NAME)})
    cursor = str_arg(a, "cursor") or None

    if name == "mygov_weather_forecast":
        data = get_weather_forecast(str_arg(a, "location") or None,
                                    limit_arg(name, a))
        return envelope(name, data)

    if name == "mygov_weather_warning":
        return envelope(name, get_weather_warning())

    if name in ("mygov_data_catalogue", "mygov_opendosm"):
        dataset_id = dataset_id_arg(a)
        keys = ("filter", "contains", "sort", "date_start", "date_end")
        filters = {k: v for k, v in a.items() if k in keys and v}
        fetch = (get_data_catalogue if name == "mygov_data_catalogue"
                 else get_opendosm)
        payload = fetch(dataset_id, limit_arg(name, a), filters)
        # meta=true wraps the rows; unwrap it so `data` stays the rows and the
        # publisher's own timestamps go where provenance belongs.
        if isinstance(payload, dict) and "data" in payload:
            upstream = payload.get("meta") or {}
            rows = payload.get("data")
        else:
            upstream, rows = {}, payload
        return envelope(name, rows, dataset=dataset_id,
                        data_period=upstream.get("data_as_of"),
                        data_updated_at=upstream.get("last_updated"),
                        next_update=upstream.get("next_update"),
                        update_frequency=upstream.get("update_frequency"),
                        publisher=upstream.get("data_source"))

    if name == "mygov_dataset_info":
        api = enum_arg(name, "api", a, default="data-catalogue")
        info = get_dataset_info(api, dataset_id_arg(a))
        return envelope(name, info, dataset=info["dataset_id"],
                        data_period=info.get("data_as_of"),
                        data_updated_at=info.get("last_updated"),
                        update_frequency=info.get("update_frequency"))

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
        vehicles = get_gtfs_realtime(agency, category)
        page, paging = paginate(vehicles, limit_arg(name, a), cursor,
                                {"agency": agency, "category": category})
        return envelope(name, {"agency": agency, "category": category,
                               "live_vehicles": len(vehicles),
                               "vehicles": page, **paging})

    if name == "mygov_rapid_bus_live":
        provider = enum_arg(name, "provider", a, default="RKL", upper=True)
        route = str_arg(a, "route")
        if route and not re.match(r"^[A-Za-z0-9-]{1,16}$", route):
            raise invalid("route must be 1-16 letters, digits or hyphens "
                          "(e.g. 'T200', '300')", route=route)
        buses, context = get_rapid_bus_live(provider, route)
        page, paging = paginate(buses, limit_arg(name, a), cursor,
                                {"provider": provider, "route": route})
        return envelope(name, {"provider": provider, "route": route or "all",
                               "live_buses": len(buses), **context,
                               "buses": page, **paging})

    if name == "mygov_flood_risk":
        data = get_flood_risk()
        return envelope(name, data, data_updated_at=data.get("updated"))

    if name == "mygov_pricecatcher":
        item = str_arg(a, "item")
        group = enum_arg(name, "group", a, default="") or ""
        rows, context = get_pricecatcher(item, group)
        page, paging = paginate(rows, limit_arg(name, a), cursor,
                                {"item": item.lower(), "group": group})
        return envelope(name, {**context, "items": page, **paging},
                        data_period=context.get("as_of"),
                        data_updated_at=context.get("generated"))

    if name == "mygov_tourism_arrivals":
        country = str_arg(a, "country")
        rows, context = get_tourism(country)
        page, paging = paginate(rows, limit_arg(name, a), cursor,
                                {"country": country.lower()})
        return envelope(name, {**context, "countries": page, **paging},
                        data_period=context.get("as_of"),
                        data_updated_at=context.get("generated"))

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
        category, state = str_arg(a, "category"), str_arg(a, "state")
        query = str_arg(a, "query")
        rows, context = get_election_results(category, state, query)
        page, paging = paginate(rows, limit_arg(name, a), cursor,
                                {"category": category.lower(),
                                 "state": state.upper(), "query": query.lower()})
        return envelope(name, {**context, "seats": page, **paging},
                        data_updated_at=context.get("generated"))

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
                "serverInfo": {"name": "mygov-api-mcp", "version": SERVER_VERSION},
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
                _trace_reset()
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
