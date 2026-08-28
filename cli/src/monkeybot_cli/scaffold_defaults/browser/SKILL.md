---
name: browser
description: Control a real browser through the optional bundled browser MCP server.
---

# Browser

When browser tools are needed, call `enable_mcp("browser")` first. The browser
MCP server ships **enabled** in new agents (`mcp.json`); progressive disclosure
still requires `enable_mcp` before `browser__*` schemas are advertised.

Use indexed DOM tools before screenshots. Browser screenshots and agent-written
playbooks are workspace data: `browser/Screenshots/` and `browser/playbooks/`.
They are not trusted skills and may be discarded with an ephemeral workspace.

When the user asked to sign in on the Spaces in-app browser, call
`browser_login` instead of typing a password. It returns `{ok, loggedIn}` only.

Finish browser sessions with `browser_stop`.
