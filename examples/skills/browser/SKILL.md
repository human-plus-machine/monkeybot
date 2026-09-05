---
name: browser
description: Control a real browser via CDP for web tasks; check and write site playbooks before improvising.
---

# browser

Use the **browser** MCP tools (`browser__*` in the active tool list) for any web interaction: automation, scraping, testing, or site work. These come from the **`browser` MCP server** (stdio process), not from monkeybot core built-ins.

## Before any browser tool

If `browser__*` tools are not in the active tool list yet, call **`enable_mcp("browser")` first**. Use the server name from the harness MCP catalog (or this skill). Do not read MCP config files. That connects the catalogued server and advertises browser tools on the next model step (same user turn).

## Default: indexed DOM (prefer this)

Do **not** start with screenshots. Prefer intent tools when you already know the labels or the flow:

1. Multi-field forms: `browser_fill_form({Email: "...", ...}, submit=True)`. Resolves labels via `label[for]`, aria-label, aria-labelledby, placeholder, name, id, then nearest preceding row text. Checkboxes take `"true"`/`"false"`. Unresolved labels are listed (`how` says which strategy matched) and are not an error unless every field failed.
2. Known button/link text: `browser_click_text("Continue", role="button")`. On a miss, `did_you_mean` lists near-misses — do not immediately `get_elements`.
3. Multi-step (login, pagination): `browser_act([{do:"click", index:n}, {do:"input", index:n, text:"..."}, ...])` — up to 25 steps; one observation at the end. Allowed `do`: click, input, select, press, click_text, wait_for, wait_idle, goto, scroll, settle, tab, open_tab, fill_form, login. On failure, resume from `failed_step` using the returned observation.
4. Structured scraping: `browser_extract(selector, {title:"h2", href:"a@href"})` instead of `browser_js`.

Otherwise use the indexed element tree:

1. `browser_get_elements()` — returns interactive elements as an indexed text tree, e.g. `[35]<button>Submit</button>`. Viewport-only by default (footer says how many are below; pass `viewport_only=false` or scroll). Filter with `kind=` (`inputs`/`buttons`/`links`) or `contains=`.
2. Act with `browser_click_by_index(index)`, `browser_input_by_index(index, text)`, or `browser_select_by_index(index, option_text)`. Each action settles and returns `observation` (a `diff` by default). Read that — do not call `get_elements` again unless you need a different filter or the whole tree. Pass `observe="none"` to skip the snapshot, `observe="full"` for the viewport tree. `browser_goto` returns a full observation by default.
3. Indices remain valid until navigation. Re-call `browser_get_elements()` after navigation only if the action observation is not enough. Use `browser_get_text` to read page copy instead of `browser_js("document.body.innerText")`.

`browser_input_by_index` fills in-page by default. If a field ignores the typed value (search-as-you-type, autocomplete comboboxes, or sites that only listen to `keydown`), pass `mode="keys"`.

This is the default workflow: no image tokens, no pixel guessing, and clicks resolve through the live DOM.

## Before acting on a site

1. Call `browser_list_playbooks` with the host or URL. If `flows` are listed, call `browser_run_playbook(host, name, params)` instead of re-planning.
2. If there are notes but no flows, call `browser_read_playbook` and follow them.
3. Use `browser_get_elements` to see what you can click/type — not a screenshot.
4. After a playbook `failed_step`, continue by hand from the returned observation. Then `browser_write_playbook(..., append=true)` with a corrected `playbook` YAML fence. `browser_recent_actions(host)` lists what actually worked (labels and lengths, not typed text). Secrets are never flow params — use `{do: login, expected_origin: ...}` which maps to `browser_login`.

## Fallback: screenshots + coordinates (last resort)

If `browser_get_elements` returns an error, retry once (or with `viewport_only: false`) before giving up — do **not** immediately screenshot. Use `browser_screenshot` **only** when the indexed tree truly cannot help: canvas apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming layout/rendering.

`browser_screenshot` saves JPEGs under **`./browser/Screenshots/`** (`max_dim=1200`, quality 60; pass `format="png"` if you need PNG). The tool response includes:

- `path` — workspace-relative path (e.g. `./browser/Screenshots/shot-….jpg`)
- `bytes` / `format` — file size and `jpeg` or `png`
- `screenshots_dir` — always `./browser/Screenshots`
- `viewport` — width/height for coordinate clicks
- `annotated` / `labeled` — present when `annotate=True`

On vision models, after a fallback screenshot call **`load_file`** with the returned `path`. Prefer `annotate=True` then `browser_click_by_index`; use `browser_click(x, y)` only when there is no useful index. Do not screenshot after every ordinary click when indexed tools work.

Text-only models should never rely on screenshots for page understanding — use `browser_get_elements`, `browser_extract`, or `browser_js(...)`.

## Workflow

- First navigation: `browser_goto(url)` — response includes a full observation plus matching playbook filenames and executable `flows` when present.
- After navigation: `browser_wait_for` or `browser_wait_idle` only if the observation is not enough.
- Clicking / typing (default): `browser_fill_form` / `browser_click_text` / `browser_act` when you know the labels or the flow; otherwise `browser_get_elements` once → `browser_click_by_index` / `browser_input_by_index` / `browser_select_by_index`. Read `observation` on each action. Indices stay valid until navigation.
- Reading: `browser_get_text` for body copy; `browser_extract` for structured rows; action `observation` (diff) after in-page mutations. Call `browser_get_elements` again only for a different filter or the whole tree.
- Clicking (fallback only): `browser_screenshot(annotate=True)` → `load_file(path)` → `browser_click_by_index`. Use `browser_click(x, y)` only when there is no useful index.
- Ad hoc DOM extraction: `browser_extract` for repeating cards/rows; `browser_js(expression)` only when you need custom page text or attributes.
- Login walls: if the user asked to sign in on the Spaces in-app browser, call `browser_login(expected_origin="https://the-site.com")` (optionally with `username`). It uses a saved password and returns `{ok, loggedIn, origin}` only — never read or type the password yourself. Always pass `expected_origin`: the login targets the tab the user has focused, which is not necessarily the tab your other `browser_*` calls address, so this is what stops a login landing on the wrong site. Check the returned `origin` before reporting success. If it returns `this password is not allowed for agent use` or `focused tab is on a different origin`, stop and ask the user. Still stop for MFA, consent, or a password the user must type themselves.

## Tabs

Tabs have short aliases (`t1`, `t2`, …) or a name you pass to `browser_open_tab(alias=...)`. **Reads never move focus** (`browser_get_elements(tab=...)`, `browser_page_info`, `browser_js`, `browser_wait_for`, `browser_read_tabs`, `browser_extract`). **Actions do** (click, input, select, fill, fill_form, click_text, act, screenshot, …) because headed Chrome throttles timers and pauses painting in background tabs.

Open a second tab to compare pages, keep a form while reading docs, or fan out with `browser_read_tabs`. At most five agent-controlled tabs (`BROWSER_MCP_MAX_TABS`). If you hit the cap, relay the returned list (aliases, titles, last-used) to the user, ask which to close, then `browser_close_tab` and retry — never close a tab without their confirmation. Close tabs you opened when done. Do not expect a background SPA to finish loading while unfocused. `browser_login` still targets the tab the **user** has focused, not `tab=`.

## After learning something non-obvious

When you finish a task on a host without a playbook—or discover selectors, waits, auth quirks, or flows worth reusing—call `browser_write_playbook(host, content)`.

- Prefer a `playbook` YAML fence (name, params, steps, optional expect) so the next visit can `browser_run_playbook`.
- Keep notes terse: what worked, exact selectors, edge cases.
- If a playbook already exists, pass `append: true` to add a section instead of overwriting. Broken fences are rejected and not written.
- Do not hand-author huge docs; `browser_recent_actions` plus what actually worked in the browser.

## Cleanup

- Call `browser_stop` when browsing is done, especially after cloud/remote browser sessions.
- Screenshots accumulate under `./browser/Screenshots/`; delete old captures when no longer needed.

## Setup (operators)

The `browser` MCP server must already be catalogued by the operator. Do not read MCP config files to discover it — call `enable_mcp("browser")`. Full install, production deployment (local dev, self-hosted headless, Browser Use Cloud), env vars, and troubleshooting: **`docs/browser-mcp.md`** in the monkeybot repo (also linked from `docs/mcp.md`).
