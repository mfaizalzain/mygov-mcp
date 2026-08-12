# claude-mygov — Claude Code plugin

Bundles the mygov MCP server (api.data.gov.my) for Claude Code. Ships the server
dependency-free (stdlib only) and a `mygov-data` skill.

## Tools (14)

`mygov_weather_forecast`, `mygov_weather_warning`, `mygov_data_catalogue`,
`mygov_opendosm`, `mygov_gtfs_static_summary`, `mygov_gtfs_realtime`,
`mygov_rapid_bus_live`, `mygov_flood_risk`, `mygov_pricecatcher`,
`mygov_tourism_arrivals`, `mygov_rapid_service_alert`, `mygov_air_quality`,
`mygov_hotel_performance` (quarterly hotel occupancy/room rate/guests by
state, Tourism Malaysia), `mygov_election_results` (latest SPR election
results: PRU-15, state elections for all 13 states, latest by-election —
filter by category/state/search).

## Transports

```bash
python3 servers/server.py              # stdio (what plugins use)
python3 servers/server.py --http       # http://127.0.0.1:8765/mcp + /health
python3 servers/server.py --health     # probe every source, exit non-zero if degraded
```

`--http` binds to localhost and rejects unknown browser Origins (pass
`--allow-origin` to widen). Serve it publicly only behind a proxy you trust.

## Tests

```bash
python3 -m unittest discover -s tests            # offline, no network
MYGOV_LIVE=1 python3 -m unittest discover -s tests   # + real upstreams
```

## Local testing

```bash
claude plugin validate ./claude-mygov --strict
# optional: test as a plugin in a session
claude --plugin-dir ./claude-mygov
```

## Publishing (community marketplace)

1. `claude plugin validate ./claude-mygov --strict` must print `✔ Validation passed`.
2. Push this repo to GitHub (plugin is referenced by commit SHA).
3. Submit via the Console form: **platform.claude.com/plugins/submit**
   (individual authors) — or the claude.ai form if you have Team/Enterprise.
4. Review is automated safety screening + validation re-run.
5. Approved → pinned in `anthropics/claude-plugins-community`; CI bumps the pin
   on new commits; catalog syncs nightly.

Users then install with:

```bash
/plugin marketplace add anthropics/claude-plugins-community
```

and search `@claude-community` for mygov.

## Notes

- MCP server command uses `${CLAUDE_PLUGIN_ROOT}` so the bundled server resolves
  wherever the plugin lands.
- `claude plugin validate` needs Claude Code installed; it does NOT need an
  Anthropic login on this machine — run it via the opencodex proxy wrapper if plain
  `claude` isn't authenticated.
