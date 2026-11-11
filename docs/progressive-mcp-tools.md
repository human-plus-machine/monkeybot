# Progressive MCP tool disclosure

**Status:** implemented  
**Related:** [MCP](mcp.md) · [Features — MCP](features.md) · [Browser MCP](browser-mcp.md) · [Progressive loop tools](progressive-loop-tools.md)

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

## Idle hiding

A connected server that goes unused for 10 inner turns is due to be hidden, but its tools stay in the provider payload until the next history rewrite (compaction or the load-max truncation). That rewrite already replaces the cached message prefix, so dropping the schemas there does not add a second cache break. The idle note is attached to the next tool result, and `enable_mcp` with that server's name brings the tools back. A use before the rewrite cancels the hide.
