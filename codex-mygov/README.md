# codex-mygov — Codex / ChatGPT plugin

Bundles the mygov MCP server (api.data.gov.my) for Codex CLI and ChatGPT Work.
Ships the server dependency-free (stdlib only) and a `mygov-data` skill.

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

1. **Host the server publicly** — expose the MCP server as streamable HTTP, e.g.
   an `/mcp` route on the mygov Cloudflare Worker (mygov.faizalmzain.com).
2. **Domain verification** — host the portal token at
   `https://mygov.faizalmzain.com/.well-known/openai-apps-challenge`.
3. **Verify identity** — individual or business verification in OpenAI Platform,
   plus `Apps Management: Write` on your org role.
4. Submit at **platform.openai.com/plugins** → Create plugin → With MCP:
   - Info tab: listing, logo, website/support/privacy/terms URLs
   - MCP tab: server URL → **Scan Tools** → review 6 tools (all `readOnlyHint: true`)
   - Prompts: 3+ starter prompts; Testing: 5 positive + 3 negative test cases
   - Global: country availability; Submit: release notes + attestations
5. Approve → you choose when to publish → appears in the universal Plugins
   Directory (ChatGPT Work + Codex CLI `/plugins`).

## Notes

- The local `.mcp.json` uses `${PLUGIN_ROOT}` so the bundled server resolves in
  the installed plugin.
- All 6 tools are read-only — the server already advertises
  `readOnlyHint: true` / `openWorldHint: false` / `destructiveHint: false`.
