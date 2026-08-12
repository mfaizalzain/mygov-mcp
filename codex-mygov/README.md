# codex-mygov — Codex / ChatGPT plugin

Bundles the mygov MCP server (api.data.gov.my) for Codex CLI and ChatGPT Work.
Ships the server dependency-free (stdlib only) and a `mygov-data` skill.

## Tools (14)

`mygov_weather_forecast`, `mygov_weather_warning`, `mygov_data_catalogue`,
`mygov_opendosm`, `mygov_gtfs_static_summary`, `mygov_gtfs_realtime`,
`mygov_rapid_bus_live`, `mygov_flood_risk`, `mygov_pricecatcher`,
`mygov_tourism_arrivals`, `mygov_rapid_service_alert`, `mygov_air_quality`,
`mygov_hotel_performance` (quarterly hotel occupancy/room rate/guests by
state, Tourism Malaysia), `mygov_election_results` (latest SPR election
results: PRU-15, state elections for all 13 states, latest by-election —
filter by category/state/search).

## Local testing (Codex CLI)

```bash
codex plugin marketplace add ./mygov-mcp   # from parent dir, or:
codex plugin marketplace add /Users/faizalzain/Desktop/github-repos/mygov-mcp
codex plugin marketplace list
codex /plugins                              # browse, install "mygov"
```

Or test the server directly:

```bash
codex mcp add mygov -- python3 ./servers/server.py
codex mcp list
```

## Publishing (universal directory)

The OpenAI portal scans your MCP server over **HTTPS** — a stdio-only bundle is
not enough for the public directory. You need:

1. **Host the server publicly** — already done: the mygov-mcp Worker serves
   streamable HTTP at `https://mygov-mcp.faizalmzain.com/mcp` and
   `https://mcp.malaysia-at-a-glance.com/mcp` (same Worker, two custom domains).
2. **Domain verification** — host the portal token at
   `https://mcp.malaysia-at-a-glance.com/.well-known/openai-apps-challenge`
   (the Worker serves it from the `OPENAI_CHALLENGE_TOKEN` secret).
3. **Verify identity** — individual or business verification in OpenAI Platform,
   plus `Apps Management: Write` on your org role.
4. Submit at **platform.openai.com/plugins** → Create plugin → With MCP:
   - Info tab: listing, logo, website/support/privacy/terms URLs
   - MCP tab: server URL → **Scan Tools** → review 14 tools (all `readOnlyHint: true`)
   - Prompts: 3+ starter prompts; Testing: 5 positive + 3 negative test cases
   - Global: country availability; Submit: release notes + attestations
5. Approve → you choose when to publish → appears in the universal Plugins
   Directory (ChatGPT Work + Codex CLI `/plugins`).

## Notes

- The local `.mcp.json` uses `${PLUGIN_ROOT}` so the bundled server resolves in
  the installed plugin.
- All 6 tools are read-only — the server already advertises
  `readOnlyHint: true` / `openWorldHint: false` / `destructiveHint: false`.
