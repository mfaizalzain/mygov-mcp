# Malaysian Government Open Data Connector (mygov)

You have access to Malaysian government open data via the `mygov` MCP tools (powered by `api.data.gov.my` and OpenDOSM). All tools are read-only, idempotent, and automatically throttled to 4 requests per minute per API family.

## Available Tools & Use Cases

1. **Discovery & Diagnostics**
   - `mygov_search`: **Start here when unsure about a dataset ID.** Search across 470+ data.gov.my and OpenDOSM datasets.
   - `mygov_dataset_info`: Inspect metadata, column schema, update frequency, and latest sample row before querying a catalogue.
   - `mygov_health`: Check server and upstream health (`probe=true` runs end-to-end latency checks on all data sources).

2. **Live Conditions & Weather**
   - `mygov_weather_forecast`: 7-day forecast from MET Malaysia. Filter by `location` (e.g. "Kota Bharu", "Langkawi", "Kuala Lumpur").
   - `mygov_weather_warning`: Active severe weather warnings (rain, thunderstorm, strong wind).
   - `mygov_flood_risk`: JPS water-level telemetry across Malaysia; lists monitoring stations currently in alert, warning, or danger.
   - `mygov_air_quality`: Live US AQI and PM2.5 readings for 18 major Malaysian cities. AQI >= 101 indicates unhealthy conditions.

3. **Economics, Prices & Tourism**
   - `mygov_data_catalogue`: Query national datasets such as `fuelprice` (RON95, RON97, Diesel), `exchangerates`, `interestrates`, `poskod`.
   - `mygov_opendosm`: Department of Statistics Malaysia (DOSM) time-series data: `cpi_core` (CPI), `ipi`, `ppi`, `sppi`, `iowrt`.
   - `mygov_pricecatcher`: KPDN grocery basket price comparison across districts (cheapest vs most expensive).
   - `mygov_tourism_arrivals`: Monthly international tourist arrivals by nationality (Tourism Malaysia).
   - `mygov_hotel_performance`: Quarterly hotel occupancy rate, room rates, and domestic/international guest numbers by state.
   - `mygov_election_results`: Official Election Commission (SPR) results for PRU-15 (parliament), 13 state elections (DUN), and by-elections.

4. **Public Transport & Mobility**
   - `mygov_rapid_bus_live`: Live GPS vehicle positions for Rapid KL, Rapid Penang, and Rapid Kuantan buses.
   - `mygov_rapid_service_alert`: Live service disruption alerts from Prasarana PULSE.
   - `mygov_gtfs_realtime`: Live vehicle positions from KTMB trains, Prasarana rail, and mybas services.
   - `mygov_gtfs_static_summary`: Static schedule summary for KTMB, Prasarana, and regional mybas systems.

## Query & Filtering Guidelines

- **Sorting**: For time-series like fuel prices, sort descending to get the newest data (e.g. `sort="-date"`).
- **Filtering syntax**: Use `value@column` (e.g. `contains="Kota Bharu@location_name"`).
- **Date ranges**: Use `YYYY-MM-DD@date` (e.g. `date_start="2026-01-01@date"`).
- **Pagination**: Use `cursor` provided in responses when `has_more` is true.

## Response Interpretation

- Always cite the data source and publish timestamp (`meta.source` and `meta.data_updated_at` / `meta.data_period`).
- Distinctly differentiate between when the query was made (`retrieved_at`) and what period the statistical data represents (`data_period`).
