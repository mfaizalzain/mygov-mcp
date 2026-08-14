# mygov — Gemini Plugin & Extension

Connect Google Gemini models to Malaysia Government Open Data (`api.data.gov.my` & OpenDOSM) across 17 read-only tools covering weather, economics, transport, prices, and public services.

## Installation & Setup

### 1. Gemini CLI Extension
Install the extension directly into the Gemini CLI:

```bash
# Local installation from this repo
gemini extensions link ./gemini-mygov

# Or install from GitHub repository
gemini extensions install https://github.com/mfaizalzain/mygov-mcp --path gemini-mygov
```

### 2. Antigravity / Google Agentic Workspaces
Add to your workspace or global MCP configuration (`~/.gemini/config/mcp_config.json` or project `mcp_config.json`):

```json
{
  "mcpServers": {
    "mygov": {
      "command": "python3",
      "args": ["/path/to/mygov-mcp/gemini-mygov/servers/server.py"]
    }
  }
}
```

Or connect directly to the hosted Cloudflare Worker over HTTP:

```json
{
  "mcpServers": {
    "mygov": {
      "serverUrl": "https://mygov-mcp.faizalmzain.com/mcp"
    }
  }
}
```

### 3. Google AI Studio / Gemini Custom App (OpenAPI)
When building a custom Gemini application or function-calling tool in Google AI Studio:
1. Open Google AI Studio / Vertex AI Studio.
2. Under **Tools**, select **Add Function / Import OpenAPI Spec**.
3. Point to the hosted OpenAPI endpoint: `https://mygov-mcp.faizalmzain.com/openapi.json`.
4. Copy the system prompt from [`GEMINI.md`](./GEMINI.md) into the model's System Instructions.

### 4. Custom Gemini Gem (gemini.google.com)
1. Go to Gemini > **Gem Manager** > **Create Gem**.
2. Name: `Malaysia Open Data Assistant`.
3. Paste the contents of [`GEMINI.md`](./GEMINI.md) into the Instructions.
4. If connecting via an extension / webhook, point to `https://mygov-mcp.faizalmzain.com`.
