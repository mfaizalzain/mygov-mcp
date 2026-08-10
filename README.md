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

## Tools (all read-only, annotated `readOnlyHint: true`)

- `mygov_weather_forecast` — 7-day MET Malaysia forecast, optional location filter
- `mygov_weather_warning` — active severe-weather warnings
- `mygov_data_catalogue` — general datasets (fuelprice, exchangerates, interestrates, poskod)
- `mygov_opendosm` — DOSM economics (cpi_core, ipi, ppi, sppi, iowrt)
- `mygov_gtfs_static_summary` — GTFS schedule ZIP summary (ktmb, prasarana, mybas-*)
- `mygov_gtfs_realtime` — live vehicle positions (KTMB trains, Prasarana buses/rail, mybas)
