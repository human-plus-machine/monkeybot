---
name: browser
description: Control a real browser through the optional bundled browser MCP server.
---

# Browser

When browser tools are needed, call `enable_mcp("browser")` first. Use the
name from the harness MCP catalog — do not read config files. Progressive
disclosure still requires `enable_mcp` before `browser__*` schemas are advertised.

Use indexed DOM tools before screenshots. Browser screenshots and agent-written
playbooks are workspace data: `browser/Screenshots/` and `browser/playbooks/`.
They are not trusted skills and may be discarded with an ephemeral workspace.

`browser_input_by_index` fills in-page by default; pass `mode="keys"` for
comboboxes and fields that only listen to keydown.

When the user asked to sign in on the Spaces in-app browser, call
`browser_login` instead of typing a password. It returns `{ok, loggedIn, origin}`
only. Pass `expected_origin` so a login cannot land on the wrong site, and check
the returned `origin` before reporting success.

Finish browser sessions with `browser_stop`.
