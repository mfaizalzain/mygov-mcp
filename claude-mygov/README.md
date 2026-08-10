# claude-mygov — Claude Code plugin

Bundles the mygov MCP server (api.data.gov.my) for Claude Code. Ships the server
dependency-free (stdlib only) and a `mygov-data` skill.

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
