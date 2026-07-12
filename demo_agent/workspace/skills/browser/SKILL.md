---
name: browser
description: Control a real browser via CDP for web tasks; check and write site playbooks before improvising.
---

# browser

Use the **browser** MCP tools (`browser__*` in the active tool list) for any web interaction: automation, scraping, testing, or site work. These come from the **`browser` MCP server** (stdio process), not from monkeybot core built-ins.

## Before any browser tool

If `browser__*` tools are not in the active tool list yet, call **`enable_mcp("browser")` first**. That connects the configured server from `mcp.json` and advertises browser tools on the next model step (same user turn). Do not invent `uv`/`command` args via `add_mcp_server` for this server.

## Default: indexed DOM (prefer this)

Do **not** start with screenshots. Use the indexed element tree:

1. `browser_get_elements()` — returns interactive elements as an indexed text tree, e.g. `[35]<button>Submit</button>`
2. Act with `browser_click_by_index(index)`, `browser_input_by_index(index, text)`, or `browser_select_by_index(index, option_text)`
3. Call `browser_get_elements()` again after navigation or any action that may have changed the DOM — indices are only valid for the tree they came from

This is the default workflow: no image tokens, no pixel guessing, and clicks resolve through the live DOM.

## Before acting on a site

1. Call `browser_list_playbooks` with the host or URL.
2. If a playbook exists, call `browser_read_playbook` and follow it before inventing flows.
3. Use `browser_get_elements` to see what you can click/type — not a screenshot.

## Fallback: screenshots + coordinates (last resort)

If `browser_get_elements` returns an error, retry once (or with `viewport_only: false`) before giving up — do **not** immediately screenshot. Use `browser_screenshot` + `browser_click(x, y)` **only** when the indexed tree truly cannot help: canvas apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming layout/rendering.

`browser_screenshot` saves PNGs under **`./browser/Screenshots/`**. The tool response includes:

- `path` — workspace-relative path (e.g. `./browser/Screenshots/shot-….png`)
- `screenshots_dir` — always `./browser/Screenshots`
- `viewport` — width/height for coordinate clicks

On vision models, after a fallback screenshot call **`render_image`** with the returned `path`, then `browser_click(x, y)`. Do not screenshot after every ordinary click when indexed tools work.

Text-only models should never rely on screenshots for page understanding — use `browser_get_elements` or `browser_js(...)`.

## Workflow

- First navigation: `browser_goto(url)` — response includes matching playbook filenames when present.
- After navigation: `browser_wait_for` or `browser_wait_idle` as needed.
- Clicking / typing (default): `browser_get_elements` → `browser_click_by_index` / `browser_input_by_index` / `browser_select_by_index` → `browser_get_elements` again if the page changed.
- Clicking (fallback only): `browser_screenshot` → `render_image(path)` → `browser_click(x, y)`.
- Ad hoc DOM extraction: `browser_js(expression)` when you need custom page text or attributes.
- Login walls: stop and ask the user. Use SSO only when Chrome is already signed in; still stop for passwords, MFA, or consent.

## After learning something non-obvious

When you finish a task on a host without a playbook—or discover selectors, waits, auth quirks, or flows worth reusing—call `browser_write_playbook(host, content)`.

- Keep notes terse: what worked, exact selectors, edge cases.
- If a playbook already exists, pass `append: true` to add a section instead of overwriting.
- Do not hand-author huge docs; record what actually worked in the browser.

## Cleanup

- Call `browser_stop` when browsing is done, especially after cloud/remote browser sessions.
- Screenshots accumulate under `./browser/Screenshots/`; delete old captures when no longer needed.

## Setup (operators)

Add the `browser` MCP server to `monkeybot_config/mcp.json`. Schemas are not advertised until the agent calls `enable_mcp("browser")`. Full install, production deployment (local dev, self-hosted headless, Browser Use Cloud), env vars, and troubleshooting: **`docs/browser-mcp.md`** in the monkeybot repo (also linked from `docs/mcp.md`).
