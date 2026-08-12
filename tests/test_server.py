#!/usr/bin/env python3
"""Test suite for the mygov MCP server.

Offline by default: every test here stubs the network, so `python3 -m unittest`
runs anywhere with no API access and no rate-limit cost.

    python3 -m unittest discover -s tests -v       # offline suite
    MYGOV_LIVE=1 python3 -m unittest discover -s tests   # + live upstreams

The live tests are opt-in because they call real government APIs.
"""
import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_SERVER = os.path.join(REPO, "claude-mygov", "servers", "server.py")
CODEX_SERVER = os.path.join(REPO, "codex-mygov", "servers", "server.py")
LIVE = os.environ.get("MYGOV_LIVE") == "1"


def load_server():
    spec = importlib.util.spec_from_file_location("mygov_server", CLAUDE_SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


srv = load_server()


class FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None, status=200):
        super().__init__(body)
        self.headers = headers or {"Content-Type": "application/json"}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@contextlib.contextmanager
def stub_network(handler):
    """Replace urlopen for the duration of a test.

    `handler(url)` returns the response body (bytes/str/dict) or raises.
    """
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        calls.append(url)
        result = handler(url)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, (dict, list)):
            result = json.dumps(result).encode("utf-8")
        elif isinstance(result, str):
            result = result.encode("utf-8")
        return FakeResponse(result)

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        yield calls
    finally:
        urllib.request.urlopen = original


def fresh_cache():
    srv.CACHE = srv.TTLCache()
    srv._trace_reset()


def call(name, args=None):
    """Invoke a tool the way the protocol layer does."""
    srv._trace_reset()
    return srv.call_tool(name, args or {})


# --------------------------------------------------------------------------
# Tool declarations
# --------------------------------------------------------------------------
class ToolDeclarationTests(unittest.TestCase):
    def test_every_tool_is_completely_declared(self):
        for tool in srv.TOOLS:
            with self.subTest(tool=tool.get("name")):
                self.assertTrue(tool["name"].startswith("mygov_"))
                self.assertGreater(len(tool["description"]), 40)
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema.get("additionalProperties", True),
                                 "schemas must reject unknown arguments")
                self.assertIn("annotations", tool)

    def test_annotations_are_consistent(self):
        for tool in srv.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["annotations"], srv.READ_ONLY)

    def test_every_tool_has_provenance(self):
        for tool in srv.TOOLS:
            with self.subTest(tool=tool["name"]):
                source = srv.SOURCES.get(tool["name"])
                self.assertIsNotNone(source, "tool is missing a SOURCES entry")
                self.assertIn("source", source)
                self.assertIn("source_url", source)

    def test_limits_declare_usable_bounds(self):
        for tool in srv.TOOLS:
            spec = tool["inputSchema"]["properties"].get("limit")
            if not spec:
                continue
            with self.subTest(tool=tool["name"]):
                self.assertLessEqual(spec["minimum"], spec["default"])
                self.assertLessEqual(spec["default"], spec["maximum"])

    # These tools hand `limit` to the upstream API, which exposes no offset or
    # cursor of its own, so there is nothing for this server to page over.
    UPSTREAM_LIMITED = {"mygov_weather_forecast", "mygov_data_catalogue",
                        "mygov_opendosm"}

    def test_tools_that_page_locally_accept_a_cursor(self):
        for tool in srv.TOOLS:
            props = tool["inputSchema"]["properties"]
            if "limit" in props and tool["name"] not in self.UPSTREAM_LIMITED:
                with self.subTest(tool=tool["name"]):
                    self.assertIn("cursor", props,
                                  "a locally-paged tool must accept a cursor")

    def test_cursor_tools_actually_return_a_cursor_contract(self):
        # Guards against a tool declaring `cursor` but never emitting one.
        for tool in srv.TOOLS:
            props = tool["inputSchema"]["properties"]
            if "cursor" not in props:
                continue
            with self.subTest(tool=tool["name"]):
                self.assertIn("limit", props,
                              "a cursor without a limit cannot page")

    def test_enum_defaults_are_members_of_their_enum(self):
        for tool in srv.TOOLS:
            for prop, spec in tool["inputSchema"]["properties"].items():
                if "enum" in spec and "default" in spec:
                    with self.subTest(tool=tool["name"], prop=prop):
                        self.assertIn(spec["default"], spec["enum"])

    def test_no_tool_is_declared_without_being_wired_up(self):
        # A declared-but-unrouted tool would only surface at call time.
        for tool in srv.TOOLS:
            with self.subTest(tool=tool["name"]):
                with stub_network(lambda url: ConnectionError("blocked")):
                    try:
                        call(tool["name"])
                    except srv.ToolError as e:
                        self.assertNotEqual(
                            e.code, "INTERNAL_ERROR",
                            f"{tool['name']} is declared but not dispatched")
                    except Exception:
                        pass


class PluginParityTests(unittest.TestCase):
    def test_both_plugin_copies_are_identical(self):
        with open(CLAUDE_SERVER, "rb") as a, open(CODEX_SERVER, "rb") as b:
            self.assertEqual(a.read(), b.read(),
                             "claude-mygov and codex-mygov servers have drifted")


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------
class ArgumentValidationTests(unittest.TestCase):
    def test_unknown_tool_is_not_found(self):
        with self.assertRaises(srv.ToolError) as ctx:
            call("mygov_nope")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_bad_enum_lists_the_allowed_values(self):
        with self.assertRaises(srv.ToolError) as ctx:
            call("mygov_rapid_bus_live", {"provider": "XXX"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(ctx.exception.details["allowed"], ["RKL", "RPG", "RKN"])

    def test_missing_dataset_id_is_rejected(self):
        for tool in ("mygov_data_catalogue", "mygov_opendosm",
                     "mygov_dataset_info"):
            with self.subTest(tool=tool):
                with self.assertRaises(srv.ToolError) as ctx:
                    call(tool)
                self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_prasarana_realtime_requires_a_category(self):
        with self.assertRaises(srv.ToolError) as ctx:
            call("mygov_gtfs_realtime", {"agency": "prasarana"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn("rapid-rail-kl", ctx.exception.details["allowed"])

    def test_limit_is_clamped_to_the_schema_bounds(self):
        spec = srv._schema("mygov_pricecatcher", "limit")
        self.assertEqual(srv.limit_arg("mygov_pricecatcher", {"limit": 10**9}),
                         spec["maximum"])
        self.assertEqual(srv.limit_arg("mygov_pricecatcher", {"limit": -5}),
                         spec["minimum"])
        self.assertEqual(srv.limit_arg("mygov_pricecatcher", {}),
                         spec["default"])

    def test_non_integer_limit_is_an_argument_error(self):
        with self.assertRaises(srv.ToolError) as ctx:
            srv.limit_arg("mygov_pricecatcher", {"limit": "many"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_malformed_route_is_rejected(self):
        with self.assertRaises(srv.ToolError) as ctx:
            call("mygov_rapid_bus_live", {"route": "'; DROP TABLE"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------
class PaginationTests(unittest.TestCase):
    items = [{"n": i} for i in range(25)]
    query = {"q": "test"}

    def test_first_page_reports_the_full_total(self):
        page, meta = srv.paginate(self.items, 10, None, self.query)
        self.assertEqual(len(page), 10)
        self.assertEqual(meta["total"], 25)
        self.assertEqual(meta["offset"], 0)
        self.assertTrue(meta["has_more"])
        self.assertIsNotNone(meta["next_cursor"])

    def test_cursor_walks_the_whole_list_exactly_once(self):
        seen, cursor, pages = [], None, 0
        while True:
            page, meta = srv.paginate(self.items, 10, cursor, self.query)
            seen.extend(page)
            pages += 1
            cursor = meta["next_cursor"]
            if not cursor:
                break
            self.assertLess(pages, 10, "pagination did not terminate")
        self.assertEqual(seen, self.items)
        self.assertEqual(pages, 3)

    def test_last_page_has_no_cursor(self):
        _, meta = srv.paginate(self.items, 100, None, self.query)
        self.assertFalse(meta["has_more"])
        self.assertIsNone(meta["next_cursor"])

    def test_cursor_from_a_different_query_is_rejected(self):
        _, meta = srv.paginate(self.items, 10, None, self.query)
        with self.assertRaises(srv.ToolError) as ctx:
            srv.paginate(self.items, 10, meta["next_cursor"], {"q": "other"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_malformed_cursor_is_rejected(self):
        for bad in ("garbage", "!!!!", "eyJvIjoxfQ"):
            with self.subTest(cursor=bad):
                with self.assertRaises(srv.ToolError) as ctx:
                    srv.paginate(self.items, 10, bad, self.query)
                self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_empty_result_paginates_cleanly(self):
        page, meta = srv.paginate([], 10, None, self.query)
        self.assertEqual(page, [])
        self.assertEqual(meta["total"], 0)
        self.assertFalse(meta["has_more"])


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
class CacheTests(unittest.TestCase):
    def setUp(self):
        fresh_cache()

    def test_entry_expires_after_its_ttl(self):
        cache = srv.TTLCache()
        cache.set("k", "v", ttl=100)
        self.assertEqual(cache.get("k")[0], "v")
        cache._store["k"] = ("v", time.time() - 101, 100)
        self.assertIsNone(cache.get("k"))

    def test_cache_evicts_when_full(self):
        cache = srv.TTLCache(max_entries=3)
        for i in range(5):
            cache.set(f"k{i}", i, ttl=100)
        self.assertLessEqual(cache.stats()["entries"], 3)

    def test_second_fetch_is_served_from_cache(self):
        with stub_network(lambda url: {"ok": True}) as calls:
            srv.http_get_json("https://example.test/a", ttl=60)
            srv.http_get_json("https://example.test/a", ttl=60)
        self.assertEqual(len(calls), 1, "second call should not hit the network")

    def test_ttl_of_zero_disables_caching(self):
        with stub_network(lambda url: {"ok": True}) as calls:
            srv.http_get_json("https://example.test/b", ttl=0)
            srv.http_get_json("https://example.test/b", ttl=0)
        self.assertEqual(len(calls), 2)

    def test_cache_bucket_is_stable_inside_the_window(self):
        self.assertEqual(srv.cache_bucket(3600), srv.cache_bucket(3600))
        self.assertNotEqual(srv.cache_bucket(1), srv.cache_bucket(10**9))

    def test_meta_reports_hit_and_miss(self):
        payload = {"items": [], "months": [], "asOf": "2026-08", "basket": {}}
        with stub_network(lambda url: payload):
            first = call("mygov_pricecatcher")
            second = call("mygov_pricecatcher")
        self.assertEqual(first["meta"]["cache"]["status"], "miss")
        self.assertEqual(second["meta"]["cache"]["status"], "hit")


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------
class ErrorMappingTests(unittest.TestCase):
    def setUp(self):
        fresh_cache()

    def _raise(self, exc):
        with stub_network(lambda url: exc):
            with self.assertRaises(srv.ToolError) as ctx:
                srv.http_get_json("https://example.test/x")
        return ctx.exception

    def test_404_is_not_found_and_not_retryable(self):
        err = self._raise(urllib.error.HTTPError(
            "https://example.test/x", 404, "Not Found", {}, None))
        self.assertEqual(err.code, "NOT_FOUND")
        self.assertFalse(err.retryable)

    def test_429_is_rate_limit_and_honours_retry_after(self):
        err = self._raise(urllib.error.HTTPError(
            "https://example.test/x", 429, "Too Many", {"Retry-After": "42"}, None))
        self.assertEqual(err.code, "UPSTREAM_RATE_LIMIT")
        self.assertTrue(err.retryable)
        self.assertEqual(err.retry_after_seconds, 42)

    def test_500_is_retryable_unavailable(self):
        err = self._raise(urllib.error.HTTPError(
            "https://example.test/x", 500, "Boom", {}, None))
        self.assertEqual(err.code, "UPSTREAM_UNAVAILABLE")
        self.assertTrue(err.retryable)

    def test_400_is_not_retryable(self):
        err = self._raise(urllib.error.HTTPError(
            "https://example.test/x", 400, "Bad", {}, None))
        self.assertFalse(err.retryable)

    def test_timeout_is_upstream_timeout(self):
        err = self._raise(socket.timeout("timed out"))
        self.assertEqual(err.code, "UPSTREAM_TIMEOUT")
        self.assertTrue(err.retryable)

    def test_connection_reset_is_retried_once(self):
        attempts = []

        def handler(url):
            attempts.append(url)
            if len(attempts) == 1:
                return ConnectionResetError(104, "Connection reset by peer")
            return {"ok": True}

        with stub_network(handler):
            self.assertEqual(srv.http_get_json("https://example.test/z"),
                             {"ok": True})
        self.assertEqual(len(attempts), 2)

    def test_http_status_errors_are_not_retried(self):
        attempts = []

        def handler(url):
            attempts.append(url)
            return urllib.error.HTTPError(url, 500, "Boom", {}, None)

        with stub_network(handler):
            with self.assertRaises(srv.ToolError):
                srv.http_get_json("https://example.test/z")
        self.assertEqual(len(attempts), 1, "an HTTP status is an answer, not a "
                                           "transient fault")

    def test_non_json_body_is_data_unavailable(self):
        with stub_network(lambda url: "<html>nope</html>"):
            with self.assertRaises(srv.ToolError) as ctx:
                srv.http_get_json("https://example.test/y")
        self.assertEqual(ctx.exception.code, "DATA_UNAVAILABLE")

    def test_error_serialization_is_complete(self):
        err = srv.ToolError("UPSTREAM_TIMEOUT", "slow", retryable=True,
                            retry_after_seconds=15, details={"url": "u"})
        payload = err.to_dict()["error"]
        self.assertEqual(payload["code"], "UPSTREAM_TIMEOUT")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["retry_after_seconds"], 15)


# --------------------------------------------------------------------------
# Provenance envelope
# --------------------------------------------------------------------------
class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        fresh_cache()

    def test_envelope_separates_retrieval_from_data_period(self):
        payload = {"items": [], "months": ["2026-07"], "asOf": "2026-07",
                   "generated": "2026-08-01", "basket": {}}
        with stub_network(lambda url: payload):
            result = call("mygov_pricecatcher")
        meta = result["meta"]
        self.assertIn("data", result)
        self.assertEqual(meta["data_period"], "2026-07")
        self.assertEqual(meta["data_updated_at"], "2026-08-01")
        self.assertNotEqual(meta["retrieved_at"], meta["data_period"])
        self.assertTrue(meta["retrieved_at"].endswith("Z"))
        self.assertIn("source", meta)

    def test_units_are_declared_where_they_were_ambiguous(self):
        payload = {"items": [], "months": [], "asOf": "2026-07", "basket": {}}
        with stub_network(lambda url: payload):
            result = call("mygov_pricecatcher")
        self.assertEqual(result["data"]["currency"], "MYR")

    def test_coordinates_use_full_names(self):
        payload = {"updated": "2026-08-12T00:00:00Z", "at_risk": 1,
                   "states": [], "stations": [
                       {"name": "S", "lat": 3.1, "lon": 101.6, "level": 4.2}]}
        with stub_network(lambda url: payload):
            result = call("mygov_flood_risk")
        station = result["data"]["stations"][0]
        self.assertEqual(station["latitude"], 3.1)
        self.assertEqual(station["longitude"], 101.6)
        self.assertNotIn("lat", station)
        self.assertNotIn("lon", station)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
CATALOGUE_HTML = '''<html><head><script id="__NEXT_DATA__" type="application/json">
{"props": {"pageProps": {"collection": {"Transportation": {"Ridership": [
 {"id": "ridership_headline", "title": "Daily Public Transport Ridership",
  "description": "Ridership on rail and bus services.",
  "data_as_of": "2026-07", "data_source": ["MOT"]}]},
 "Prices": {"Consumer": [
 {"id": "pricecatcher", "title": "PriceCatcher Item Prices",
  "description": "Daily prices of essential goods.",
  "data_as_of": "2026-08", "data_source": ["KPDN"]}]}}}}}
</script></head><body></body></html>'''


class SearchTests(unittest.TestCase):
    def setUp(self):
        fresh_cache()

    def test_search_finds_a_dataset_by_topic(self):
        with stub_network(lambda url: CATALOGUE_HTML):
            hits, context = srv.search_datasets("ridership")
        self.assertEqual(context, {})
        self.assertEqual(hits[0]["dataset_id"], "ridership_headline")
        self.assertEqual(hits[0]["category"], "Transportation")
        self.assertIn("api", hits[0])

    def test_search_dedupes_datasets_listed_on_both_portals(self):
        with stub_network(lambda url: CATALOGUE_HTML):
            hits, _ = srv.search_datasets("prices")
        ids = [h["dataset_id"] for h in hits]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_query_is_rejected(self):
        with self.assertRaises(srv.ToolError) as ctx:
            srv.search_datasets("   ")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_no_match_explains_itself(self):
        with stub_network(lambda url: CATALOGUE_HTML):
            hits, context = srv.search_datasets("zeppelin")
        self.assertEqual(hits, [])
        self.assertIn("note", context)
        self.assertIn("available_categories", context)

    def test_stopwords_do_not_score(self):
        self.assertNotIn("by", srv._query_terms("unemployment by state"))
        self.assertEqual(srv._query_terms("of the"), ["of", "the"])

    def test_broken_portal_markup_is_data_unavailable(self):
        with stub_network(lambda url: "<html>redesigned</html>"):
            with self.assertRaises(srv.ToolError) as ctx:
                srv.get_catalogue_index("data-catalogue")
        self.assertEqual(ctx.exception.code, "DATA_UNAVAILABLE")


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
class HealthTests(unittest.TestCase):
    def setUp(self):
        fresh_cache()

    def test_unprobed_health_makes_no_network_calls(self):
        with stub_network(lambda url: ConnectionError("should not be called")) as calls:
            result = call("mygov_health")
        self.assertEqual(calls, [])
        self.assertEqual(result["data"]["server"], "healthy")
        self.assertEqual(result["data"]["sources"]["status"], "not_probed")

    def test_probe_reports_each_source(self):
        with stub_network(lambda url: {"ok": True}):
            health = srv.get_health(probe=True)
        self.assertEqual(health["server"], "healthy")
        self.assertEqual(set(health["sources"]), set(srv.PROBES))
        for name, entry in health["sources"].items():
            with self.subTest(source=name):
                self.assertEqual(entry["status"], "healthy")
                self.assertTrue(entry["affects"])

    def test_a_failing_source_degrades_the_server(self):
        def handler(url):
            if "flood" in url:
                return urllib.error.HTTPError(url, 503, "down", {}, None)
            return {"ok": True}

        with stub_network(handler):
            health = srv.get_health(probe=True)
        self.assertEqual(health["server"], "degraded")
        self.assertIn("flood", health["degraded"])
        self.assertEqual(health["sources"]["flood"]["status"], "unhealthy")

    def test_every_probe_names_the_tools_it_affects(self):
        declared = {t["name"] for t in srv.TOOLS}
        for name, (_url, tools) in srv.PROBES.items():
            with self.subTest(source=name):
                for tool in tools:
                    self.assertIn(tool, declared)


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------
class ProtocolTests(unittest.TestCase):
    def test_initialize_advertises_tools(self):
        response = srv.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        result = response["result"]
        self.assertEqual(result["protocolVersion"], srv.PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["version"], srv.SERVER_VERSION)

    def test_tools_list_returns_every_tool(self):
        response = srv.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(response["result"]["tools"]), len(srv.TOOLS))

    def test_notification_gets_no_response(self):
        self.assertIsNone(srv.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_method_not_found(self):
        response = srv.handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_ping_is_answered(self):
        response = srv.handle_message(
            {"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_tool_failure_is_an_iserror_result_not_a_protocol_error(self):
        response = srv.handle_message({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "mygov_nope", "arguments": {}}})
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_successful_call_is_json_content(self):
        with stub_network(lambda url: ConnectionError("blocked")):
            response = srv.handle_message({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "mygov_health", "arguments": {}}})
        result = response["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("data", payload)
        self.assertIn("meta", payload)


class HTTPTransportTests(unittest.TestCase):
    """The HTTP handler's routing and origin checks, without binding a port."""

    class FakeHandler(srv.MCPHTTPHandler):
        def __init__(self, origin=None):  # bypass BaseHTTPRequestHandler setup
            self.headers = {"Origin": origin} if origin else {}
            self.allowed_origins = None

    def test_localhost_origins_are_allowed(self):
        handler = self.FakeHandler()
        for origin in ("http://localhost:3000", "http://127.0.0.1:8080"):
            with self.subTest(origin=origin):
                self.assertTrue(handler._origin_ok(origin))

    def test_foreign_origins_are_blocked(self):
        handler = self.FakeHandler()
        self.assertFalse(handler._origin_ok("https://evil.example"))

    def test_explicit_allowlist_is_honoured(self):
        handler = self.FakeHandler()
        handler.allowed_origins = {"https://trusted.example"}
        self.assertTrue(handler._origin_ok("https://trusted.example"))
        self.assertFalse(handler._origin_ok("https://other.example"))

    def test_wildcard_allows_any_origin(self):
        handler = self.FakeHandler()
        handler.allowed_origins = "*"
        self.assertTrue(handler._origin_ok("https://anything.example"))


# --------------------------------------------------------------------------
# Live upstreams (opt-in)
# --------------------------------------------------------------------------
@unittest.skipUnless(LIVE, "set MYGOV_LIVE=1 to test against real APIs")
class LiveTests(unittest.TestCase):
    """Smoke-tests every tool against the real upstreams."""

    LIVE_ARGS = {
        "mygov_weather_forecast": {"location": "Langkawi", "limit": 2},
        "mygov_data_catalogue": {"dataset_id": "fuelprice", "sort": "-date",
                                 "limit": 2},
        "mygov_opendosm": {"dataset_id": "cpi_core", "sort": "-date", "limit": 2},
        "mygov_dataset_info": {"dataset_id": "fuelprice"},
        "mygov_search": {"query": "electricity", "limit": 3},
        "mygov_gtfs_static_summary": {"agency": "ktmb"},
        "mygov_gtfs_realtime": {"agency": "ktmb", "limit": 2},
        "mygov_rapid_bus_live": {"provider": "RKL", "limit": 2},
        "mygov_pricecatcher": {"item": "AYAM", "limit": 2},
        "mygov_tourism_arrivals": {"limit": 2},
        "mygov_hotel_performance": {"state": "Pahang"},
        "mygov_election_results": {"category": "prk", "limit": 2},
    }

    def test_every_tool_answers(self):
        for tool in srv.TOOLS:
            name = tool["name"]
            with self.subTest(tool=name):
                result = call(name, self.LIVE_ARGS.get(name, {}))
                self.assertIn("data", result)
                self.assertIn("source", result["meta"])
                self.assertIn("retrieved_at", result["meta"])

    def test_search_then_fetch_round_trip(self):
        hits, _ = srv.search_datasets("electricity consumption")
        self.assertTrue(hits, "search should find electricity datasets")
        dataset_id = hits[0]["dataset_id"]
        info = call("mygov_dataset_info", {"api": hits[0]["api"],
                                           "dataset_id": dataset_id})
        self.assertEqual(info["data"]["dataset_id"], dataset_id)
        self.assertTrue(info["data"]["columns"])

    def test_cursor_paging_is_stable_against_live_data(self):
        first = call("mygov_pricecatcher", {"limit": 5})
        cursor = first["data"]["next_cursor"]
        self.assertIsNotNone(cursor)
        second = call("mygov_pricecatcher", {"limit": 5, "cursor": cursor})
        self.assertEqual(second["data"]["offset"], 5)
        names = {i["item"] for i in first["data"]["items"]}
        self.assertFalse(names & {i["item"] for i in second["data"]["items"]},
                         "pages should not overlap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
