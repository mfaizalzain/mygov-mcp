# mygov-mcp

Malaysia government open-data connector, packaged as **official plugins** for both the
Claude Code and Codex/ChatGPT ecosystems. Bundles the dependency-free MCP server
(`servers/server.py`, stdlib only, no pip install needed) with a usage skill.

Data source: [api.data.gov.my](https://developer.data.gov.my) — no API key, CORS open,
4 req/min per family (the server throttles itself).

## What's inside

- `claude-mygov/` — Claude Code plugin (`.claude-plugin/plugin.json` + `.mcp.json`)
  for the [Claude community marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
  submission.
- `codex-mygov/` — Codex/ChatGPT plugin (`.codex-plugin/plugin.json` + `.mcp.json`)
  for the [OpenAI plugin submission portal](https://developers.openai.com/plugins/deploy/submission).
- `.agents/plugins/marketplace.json` — local Codex marketplace for testing.
- Both plugins ship the same server + `skills/mygov-data/SKILL.md` usage guide.

## Tools (17, all read-only)

Every tool is annotated `readOnlyHint: true`, `destructiveHint: false`,
`idempotentHint: true`, `openWorldHint: true`.

**Discovery**
- `mygov_search` — find datasets by topic across 470+ data.gov.my and OpenDOSM entries
- `mygov_dataset_info` — publisher, `data_as_of`, `last_updated`, `next_update`, columns
- `mygov_health` — server/cache/collector state; `probe=true` tests every upstream

**Statistics and prices**
- `mygov_data_catalogue` — general datasets (fuelprice, exchangerates, interestrates, poskod)
- `mygov_opendosm` — DOSM economics (cpi_core, ipi, ppi, sppi, iowrt)
- `mygov_pricecatcher` — KPDN grocery basket, 13-month price history
- `mygov_tourism_arrivals` — monthly visitor arrivals by nationality
- `mygov_hotel_performance` — quarterly occupancy, room rate and guests by state
- `mygov_election_results` — SPR parliamentary, state and by-election results

**Live conditions**
- `mygov_weather_forecast` — 7-day MET Malaysia forecast, optional location filter
- `mygov_weather_warning` — active severe-weather warnings
- `mygov_flood_risk` — JPS water-level telemetry, stations at danger/warning/alert
- `mygov_air_quality` — US AQI and PM2.5 for 18 cities

**Transport**
- `mygov_gtfs_static_summary` — GTFS schedule ZIP summary (ktmb, prasarana, mybas-*)
- `mygov_gtfs_realtime` — live vehicle positions (KTMB trains, Prasarana rail, mybas)
- `mygov_rapid_bus_live` — live Rapid KL/Penang/Kuantan buses from the kiosk AVL feed
- `mygov_rapid_service_alert` — latest Rapid service disruption notice

## Response contract

Every tool returns `{"data": ..., "meta": {...}}`. `meta` carries the publisher,
source URL, and three separate timestamps: `retrieved_at` (when this server
called the API), `data_period` (what the numbers describe) and
`data_updated_at` (when the publisher last refreshed). `meta.cache` reports
whether the response was served from cache and how old it is.

Failures return `{"error": {"code", "message", "retryable", ...}}` with
`isError: true`. Codes are stable: `INVALID_ARGUMENT`, `NOT_FOUND`,
`UPSTREAM_TIMEOUT`, `UPSTREAM_RATE_LIMIT`, `UPSTREAM_UNAVAILABLE`,
`DATA_UNAVAILABLE`, `INTERNAL_ERROR`.

List tools page with an opaque `cursor` and report `total` / `has_more` /
`next_cursor`.

## Running it

```bash
python3 claude-mygov/servers/server.py            # stdio (what plugins use)
python3 claude-mygov/servers/server.py --http     # http://127.0.0.1:8765/mcp + /health
python3 claude-mygov/servers/server.py --health   # probe sources, non-zero exit if degraded
```

Still stdlib-only — no pip install for any of it.

## Tests

```bash
python3 -m unittest discover -s tests                  # offline, stubs the network
MYGOV_LIVE=1 python3 -m unittest discover -s tests     # + real upstreams
```

The offline suite runs on every push and PR; the live suite runs daily on a
schedule, so a portal that changes shape shows up before a user finds it.
