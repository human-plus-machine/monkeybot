---
name: browser
description: Control a real browser via CDP for web tasks; check and write site playbooks before improvising.
---

# browser

Use the **browser** MCP tools (`browser__*` in the active tool list) for any web interaction: automation, scraping, testing, or site work. These come from the **`browser` MCP server** (stdio process), not from MonkeyBot core built-ins.

## Before acting on a site

1. Call `browser_list_playbooks` with the host or URL.
2. If a playbook exists, call `browser_read_playbook` and follow it before inventing selectors or flows.
3. Take a screenshot (`browser_screenshot`) when you need coordinates for clicks, or use `browser_js` to read visible text when the model cannot view images (e.g. Ollama text-only models).

## Non-vision models (Ollama, etc.)

`browser_screenshot` returns a **JSON metadata blob** (host path + title/url), not inline image bytes — do not expect the model to "see" the page from a screenshot alone. Prefer `browser_js(...)` to extract headings, hero copy, and nav labels when answering "what is this site about?"

## Workflow

- First navigation: `browser_goto(url)` — response includes matching playbook filenames when present.
- After navigation: `browser_wait_for` or `browser_wait_idle` as needed.
- Clicking: screenshot → read pixels → `browser_click(x, y)` → screenshot again.
- DOM extraction: `browser_js(expression)` when coordinates are the wrong tool.
- Login walls: stop and ask the user. Use SSO only when Chrome is already signed in; still stop for passwords, MFA, or consent.

## After learning something non-obvious

When you finish a task on a host without a playbook—or discover selectors, waits, auth quirks, or flows worth reusing—call `browser_write_playbook(host, content)`.

- Keep notes terse: what worked, exact selectors, edge cases.
- If a playbook already exists, pass `append: true` to add a section instead of overwriting.
- Do not hand-author huge docs; record what actually worked in the browser.

## Cleanup

- Call `browser_stop` when browsing is done, especially after cloud/remote browser sessions.

## Setup (operators)

Enable the `browser` MCP server in `monkeybot_config/mcp.json` (`enabled: true`). Full install, production deployment (local dev, self-hosted headless, Browser Use Cloud), env vars, and troubleshooting: **`docs/browser-mcp.md`** in the MonkeyBot repo (also linked from `docs/mcp.md`).
