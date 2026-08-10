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

## Query syntax

- `filter` / `contains`: `value@column` (e.g. `level@series_type`).
- `sort`: `column` or `-column` (fuelprice defaults oldest-first → always pass `-date`).
- `date_start` / `date_end`: `YYYY-MM-DD@date`.
- Nested fields: `location__location_name`.

## Gotchas

- Weather API has no coordinates — only location_id + location_name (Ds### district, St### state, Tn### town).
- The API rate-limits at 4 req/min per family; if you get 429s, wait and retry.
- Fuel data is weekly and defaults to oldest first — always `sort=-date`.
- Response text is often Bahasa Melayu ("Tiada hujan" = no rain).
