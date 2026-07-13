---
name: browser
description: Control a real browser through the optional bundled browser MCP server.
---

# Browser

When browser tools are needed, call `enable_mcp("browser")` first. The server is
bundled with every scaffold but is disabled by default in `monkeybot_config/mcp.json`.

Use indexed DOM tools before screenshots. Browser screenshots and agent-written
playbooks are workspace data: `browser/Screenshots/` and `browser/playbooks/`.
They are not trusted skills and may be discarded with an ephemeral workspace.

Finish browser sessions with `browser_stop`.
