---
name: mygov-data
description: Malaysian government open data (api.data.gov.my). Use when the user asks about Malaysia weather, fuel prices, CPI/economy, public transport schedules, or live train/bus positions.
---

# mygov data access

Use the `mygov_*` MCP tools to query live Malaysian government open data. No API key required; the server throttles to 4 req/min per family automatically.

## Tools at a glance

- `mygov_weather_forecast` — 7-day forecast; optional `location` filter ("Kota Bharu", "Langkawi"). Returns date, morning/afternoon/night text, min/max temp.
- `mygov_weather_warning` — active MET Malaysia severe-weather warnings (no args).
- `mygov_data_catalogue` — general gov datasets; known ids: `fuelprice` (RON95/97/diesel, sort=-date), `exchangerates`, `interestrates`, `poskod`.
- `mygov_opendosm` — DOSM economics; ids: `cpi_core` (CPI), `ipi`, `ppi`, `sppi`, `iowrt`.
- `mygov_gtfs_static_summary` — GTFS schedule ZIP summary: agencies ktmb, prasarana, mybas-kota-bharu, mybas-alor-setar, mybas-kuala-terengganu, mybas-johor-bahru.
- `mygov_gtfs_realtime` — live vehicles from GTFS-RT: ktmb, prasarana (category rapid-bus-kl / rapid-rail-kl), mybas-*. NOTE: the prasarana GTFS-RT feed is often empty.
- `mygov_rapid_bus_live` — **actual** live Rapid KL/Penang/Kuantan bus positions from the official myrapidbus kiosk feed (800+ buses). Providers: RKL (Klang Valley), RPG (Penang), RKN (Kuantan). Optional route filter. Use this instead of mygov_gtfs_realtime for Rapid buses.
- `mygov_flood_risk` — flood risk by area (based on Malaysia's flood danger alerts).
- `mygov_pricecatcher` — item price comparison (PriceCatcher): cheapest/most expensive districts for an item, optional item/group filter.
- `mygov_tourism_arrivals` — monthly international visitor arrivals by country of nationality (Tourism Malaysia), optional country filter.
- `mygov_rapid_service_alert` — latest Rapid KL service alert (myrapid.com.my PULSE): title, excerpt, link, posted time.
- `mygov_air_quality` — live US AQI for 18 major Malaysian cities (Open-Meteo), worst-first + cleanest. 101+ (Unhealthy) = haze alert threshold.
- `mygov_hotel_performance` — quarterly hotel performance by state (Tourism Malaysia Paid Accommodation Survey): occupancy rate, average room rate, guests (domestic/international), current quarter vs a year earlier. Optional `state` filter.
- `mygov_election_results` — latest SPR election results: PRU-15 parliamentary (208 seats), latest state election for every state (600 DUN seats), latest by-election. Filter by `category` (pru/dun/prk), `state`, or free-text `query` (constituency/winner/party).

## Query syntax

- `filter` / `contains`: `value@column` (e.g. `level@series_type`).
- `sort`: `column` or `-column` (fuelprice defaults oldest-first → always pass `-date`).
- `date_start` / `date_end`: `YYYY-MM-DD@date`.
- Nested fields: `location__location_name`.

## Response shape

Every tool returns `{"data": ..., "meta": {...}}`. Cite `meta.source` when
reporting figures, and read the timestamps carefully:

- `meta.retrieved_at` — when this server called the API (i.e. now).
- `meta.data_period` — what the numbers actually describe (e.g. `2026-06` for
  tourism). Say "June 2026 figures", not "today's figures".
- `meta.data_updated_at` — when the publisher last refreshed the data.
- `meta.freshness` — `live` / `daily` / `monthly` / `quarterly` / `static`.

Failures return `{"error": {"code", "message", "retryable", ...}}` with
`isError: true`. Codes: `INVALID_ARGUMENT` (fix the arguments — `details`
lists the allowed values), `NOT_FOUND`, `UPSTREAM_TIMEOUT`,
`UPSTREAM_RATE_LIMIT`, `UPSTREAM_UNAVAILABLE`, `DATA_UNAVAILABLE`,
`INTERNAL_ERROR`. Only retry when `retryable` is true, after
`retry_after_seconds`.

## Result sizes

List tools default to a small page (10–50) and clamp `limit` to the schema's
maximum. When a result carries `truncated: true`, `matched` tells you how many
records matched in total — narrow the filter rather than raising `limit`. For
`mygov_rapid_bus_live` always pass `route` when asking about one service.

## Gotchas

- Weather API has no coordinates — only location_id + location_name (Ds### district, St### state, Tn### town).
- The API rate-limits at 4 req/min per family; if you get 429s, wait and retry.
- Fuel data is weekly and defaults to oldest first — always `sort=-date`.
- Response text is often Bahasa Melayu ("Tiada hujan" = no rain).
