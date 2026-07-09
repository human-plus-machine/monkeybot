---
name: browser
description: Control a real browser via CDP for web tasks; check and write site playbooks before improvising.
---

# browser

Use the **browser** MCP tools (`browser__*` in the active tool list) for any web interaction: automation, scraping, testing, or site work. These come from the **`browser` MCP server** (stdio process), not from monkeybot core built-ins.

## Screenshots

`browser_screenshot` saves PNG files under **`./browser/Screenshots/`** in the agent workspace (unique filename per capture). The tool response includes:

- `path` — workspace-relative path (e.g. `./browser/Screenshots/shot-20260701T142230Z-a1b2c3d4.png`)
- `screenshots_dir` — always `./browser/Screenshots`
- `viewport` — width/height for coordinate clicks

Reuse a prior capture later with `render_image` (vision models) or `read_file` is not applicable for binary PNGs — use `render_image` with the saved `path`.

## Before acting on a site

1. Call `browser_list_playbooks` with the host or URL.
2. If a playbook exists, call `browser_read_playbook` and follow it before inventing selectors or flows.
3. Take a screenshot (`browser_screenshot`) when you need coordinates for clicks or a visual of the page.

## Vision models

After `browser_screenshot`, call **`render_image`** with the returned `path` so the model can see the page:

```json
{
  "path": "./browser/Screenshots/shot-20260701T142230Z-a1b2c3d4.png"
}
```

Then read pixels for `browser_click(x, y)`. Take another screenshot after each click when the UI may have changed.

## Non-vision models (Ollama, etc.)

`browser_screenshot` does not inline image bytes in the tool result. Do not call `render_image` if attachments/vision are unavailable. Prefer `browser_js(...)` to extract headings, hero copy, and nav labels when answering "what is this site about?" Coordinate clicks still work when you have viewport size from the screenshot JSON and element positions from JS.

## Workflow

- First navigation: `browser_goto(url)` — response includes matching playbook filenames when present.
- After navigation: `browser_wait_for` or `browser_wait_idle` as needed.
- Clicking (vision): `browser_screenshot` → `render_image(path)` → `browser_click(x, y)` → screenshot again.
- Clicking (text-only): use `browser_js` to locate elements or return bounding boxes when possible; otherwise ask the user.
- DOM extraction: `browser_js(expression)` when coordinates are the wrong tool.
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

Enable the `browser` MCP server in `monkeybot_config/mcp.json` (`enabled: true`). Full install, production deployment (local dev, self-hosted headless, Browser Use Cloud), env vars, and troubleshooting: **`docs/browser-mcp.md`** in the monkeybot repo (also linked from `docs/mcp.md`).
