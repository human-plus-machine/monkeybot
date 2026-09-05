---
name: browser
description: Smoke-test browser skill fixture.
---

# Browser

Use the browser MCP tools when the active agent has enabled the browser server.
Tabs have aliases (`t1`, `t2`); pass `tab=` on reads without moving focus.
`browser_get_elements` is viewport-first; indices remain valid until navigation.
Actions return an `observation` — read it instead of calling `get_elements` again.
Use `browser_get_text` to read page copy.
