---
name: browser
description: Control a real browser through the optional bundled browser MCP server.
---

# Browser

When browser tools are needed, call `enable_mcp("browser")` first. Use the
name from the harness MCP catalog — do not read config files. Progressive
disclosure still requires `enable_mcp` before `browser__*` schemas are advertised.

Use indexed DOM tools before screenshots. Prefer `browser_fill_form` for
multi-field forms, `browser_click_text` when the visible label is known,
`browser_act` for a batch of steps, and `browser_extract` for structured
scraping. Browser screenshots (JPEG under
`browser/Screenshots/`) and agent-written playbooks are workspace data:
`browser/Screenshots/` and `browser/playbooks/`.
They are not trusted skills and may be discarded with an ephemeral workspace.

If `browser_list_playbooks` returns `flows`, call `browser_run_playbook` instead of
re-planning. On `failed_step`, continue by hand and append a corrected
`playbook` YAML fence. Secrets only via `{do: login, expected_origin}`.
`browser_recent_actions` drafts from what actually worked.

`browser_input_by_index` fills in-page by default; pass `mode="keys"` for
comboboxes and fields that only listen to keydown. `browser_get_elements`
is viewport-first; indices remain valid until navigation. Actions return
an `observation` (diff by default) — read that instead of calling
`get_elements` again. `browser_goto` returns a full observation.
Use `browser_get_text` to read page copy.

When the user asked to sign in on the Spaces in-app browser, call
`browser_login` instead of typing a password. It returns `{ok, loggedIn, origin}`
only. Pass `expected_origin` so a login cannot land on the wrong site, and check
the returned `origin` before reporting success.

Tabs use aliases (`t1`, `t2`, …). Reads (`get_elements`, `page_info`, `js`,
`read_tabs`) never move focus; actions do. At most five agent-controlled tabs —
if you hit the cap, ask the user which to close, then `browser_close_tab`. Close
tabs you opened. `browser_login` still targets the tab the user has focused.

Finish browser sessions with `browser_stop`.
