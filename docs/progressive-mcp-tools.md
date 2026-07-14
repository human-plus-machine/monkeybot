# Progressive MCP tool disclosure

**Status:** implemented  
**Related:** [MCP](mcp.md) · [Features — MCP](features.md) · [Browser MCP](browser-mcp.md)

## Behavior

1. **Catalog by default** — servers in `mcp.json` are known but not connected (unless `"autoConnect": true`). Their `server__*` schemas stay out of the provider payload.
2. **Activate with `enable_mcp(name)`** — connects from catalog; success returns `status` + discovered tools; failure returns the error. Mid-turn refresh advertises new tools on the **next model step this turn**.
3. **Deactivate with `disable_mcp(name)`** — disconnects and drops that server’s tools (and progressive meta-tools when none remain connected).
4. **Progressive meta-tools** — `list_mcp_resources` / `read_mcp_resource` / `list_mcp_prompts` / `get_mcp_prompt` appear only while at least one MCP server is connected.

## Config flags (per server)

| Key | Meaning |
|-----|---------|
| *(omit)* | Catalog only — model calls `enable_mcp` |
| `"enabled": false` | Not catalogued; not connectable |
| `"autoConnect": true` | Connect at startup and advertise tools immediately |

## Why

Eager advertising of heavy MCP schemas (especially browser) dominates TTFT on small/local models. Keep default turns on core tools only; pay schema cost when the model opts in.
