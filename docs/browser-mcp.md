# Browser MCP

Stdio MCP server for MonkeyBot that wraps [browser-harness](https://github.com/browser-use/browser-harness) for real-browser control (CDP) and agent-writable site playbooks.

The server package and the static browser skill are bundled with every new agent.
They are present but deliberately disabled until the operator enables the browser
entry in `monkeybot_config/mcp.json`.

---

## Enable browser controls

New agents already contain this `monkeybot_config/mcp.json` entry:

```json
"browser": {
  "enabled": false,
  "command": "python",
  "args": ["-m", "browser_mcp.server"],
  "env": {
    "BU_NAME": "monkeybot",
    "BROWSER_MCP_PLAYBOOKS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/playbooks",
    "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
  }
}
```

Change `"enabled"` to `true` when the browser is approved for this agent. The
gateway exports the absolute `MONKEYBOT_WORKSPACE_ROOT` before spawning MCP
subprocesses, so the browser's writable paths do not depend on its current
directory. Startup does **not** advertise `browser__*` schemas; the model calls
`enable_mcp("browser")` first (see [Progressive MCP tool disclosure](progressive-mcp-tools.md)).

Tools appear as `browser__*` (MCP server name + tool name). The browser skill is
already in `skills/browser/`; it provides workflow instructions only. Playbooks
and screenshots are data under `workspace/browser/`, not skills. See also
[Skills](skills.md) and [Agent project layout](agent-layout.md).

---

## Deployment modes

The MCP has no built-in “headless flag.” You choose how Chrome (or a cloud browser) exposes CDP.

### 1. Local development

Use your desktop Chrome:

1. Open `chrome://inspect/#remote-debugging`.
2. Enable **Allow remote debugging for this browser instance**.
3. Accept the **Allow** popup if Chrome shows one.

No extra env vars. Inherits your logins and extensions. Not for unattended servers.

### 2. Self-hosted headless (recommended for your own prod)

Run headless Chromium with a dedicated automation profile and point `BU_CDP_URL` at it. No permission popup, no desktop required.

**Launch Chromium:**

```bash
google-chrome \
  --headless=new \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-automation \
  --no-sandbox \
  --disable-gpu
```

**`mcp.json` env:**

```json
"env": {
  "BU_NAME": "monkeybot",
  "BU_CDP_URL": "http://127.0.0.1:9222",
  "BROWSER_MCP_PLAYBOOKS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/playbooks",
  "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
}
```

**Container pattern:** build the scaffolded agent image with
`--build-arg INSTALL_CHROMIUM=1`, then start Chromium in the same pod (sidecar
or entrypoint) before the gateway so the MCP process can reach `127.0.0.1:9222`.
Without that build argument, configure a reachable CDP endpoint or Browser Use
Cloud. Avoid Snap-packaged Chromium on Linux (CDP binding issues — see
[browser-harness snap docs](https://github.com/browser-use/browser-harness/blob/main/docs/snap-linux-headless.md)).

If you already have a WebSocket URL, set `BU_CDP_WS` instead of `BU_CDP_URL`.

### 3. Browser Use Cloud (managed alternative)

No Chrome in your image. [Browser Use Cloud](https://cloud.browser-use.com/) hosts headless browsers and returns a CDP endpoint.

1. API key: [cloud.browser-use.com/new-api-key](https://cloud.browser-use.com/new-api-key)
2. Provision a browser and obtain its CDP WebSocket URL (via their API or upstream `browser-harness` CLI).
3. Configure env:

```json
"env": {
  "BU_NAME": "monkeybot-prod",
  "BROWSER_USE_API_KEY": "${BROWSER_USE_API_KEY}",
  "BU_CDP_WS": "${BU_CDP_WS}",
  "BROWSER_MCP_PLAYBOOKS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/playbooks",
  "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
}
```

Call the `browser_stop` MCP tool when browsing is done — cloud sessions bill until stopped. As a safety net, monkeybot's `MCPClient.disconnect()`/`disconnect_all()` also call `browser_stop` automatically (best-effort, 10s timeout) whenever a connected server exposes that tool, so agent crashes, abandoned conversations, and normal shutdown all stop the remote session rather than only killing the local stdio subprocess.

For syncing cookies from a local Chrome profile into cloud browsers, see [browser-harness profile-sync](https://github.com/browser-use/browser-harness/blob/main/interaction-skills/profile-sync.md).

### 4. AWS Bedrock AgentCore Browser

AgentCore's managed browser sandbox, driven directly via Playwright over a
SigV4-signed CDP WebSocket — bypasses `browser-harness` entirely for this mode
(browser-harness 0.1.x cannot send the signed headers AgentCore's Automation
endpoint requires). This backend is **only used when `BROWSER_BACKEND=agentcore`
is set and no explicit `BU_CDP_WS`/`BU_CDP_URL` is configured** — an explicit
CDP endpoint always wins, even with `BROWSER_BACKEND=agentcore` set.

**Not in this version:** Live View, custom browsers, or S3 session recording.
Only the AWS-managed system browser (`aws.browser.v1`) over the Automation
(CDP) endpoint is supported.

1. AWS credentials configured for the target account (`aws sso login`, or any
   standard credential source `boto3` picks up) with the AgentCore Browser
   permissions from [AWS's browser tool docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-tool.html).
2. Install the extra: `uv sync --extra agentcore --dev` (adds `bedrock-agentcore`,
   `boto3`, `playwright` — no `playwright install` needed; this backend only
   connects to AgentCore's already-running browser via `connect_over_cdp`, it
   never launches a local Chromium).
3. Configure env:

```json
"env": {
  "BROWSER_BACKEND": "agentcore",
  "AWS_REGION": "us-east-1",
  "BROWSER_MCP_PLAYBOOKS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/playbooks",
  "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
}
```

Optionally set `AGENTCORE_BROWSER_ID` to use a custom browser identifier
instead of the default `aws.browser.v1`. Session TTL is left at whatever the
`bedrock-agentcore` SDK/service defaults to — not configured by this backend.

Call `browser_stop` when browsing is done — like Browser Use Cloud, AgentCore
sessions bill until stopped, and the same `MCPClient.disconnect()` safety net
described above applies here too.

Manual smoke test (not part of `pytest`):

```bash
cd integrations/browser-mcp
uv sync --extra agentcore --dev
AWS_PROFILE=... AWS_REGION=us-east-1 BROWSER_BACKEND=agentcore \
  uv run python scripts/agentcore_smoke.py
```

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `BU_CDP_URL` | HTTP DevTools URL → resolved to WebSocket (self-hosted headless) |
| `BU_CDP_WS` | Direct CDP WebSocket (cloud or custom) |
| `BU_NAME` | Daemon name; separate values for parallel browser sessions |
| `BROWSER_USE_API_KEY` | Browser Use Cloud auth |
| `BROWSER_BACKEND` | Set to `agentcore` to use AWS Bedrock AgentCore Browser (only when `BU_CDP_URL`/`BU_CDP_WS` are unset) |
| `AWS_REGION` / `AWS_PROFILE` | AgentCore backend: target region and credential profile |
| `AGENTCORE_BROWSER_ID` | AgentCore backend: browser identifier (default `aws.browser.v1`) |
| `BROWSER_MCP_PLAYBOOKS_DIR` | Playbooks directory (agent-written site notes) |
| `BROWSER_MCP_SCREENSHOTS_DIR` | Screenshot output directory (default: `{workspace}/browser/Screenshots`) |
| `BROWSER_MCP_PERF` | Set to `1` to record per-tool wall time, harness-call counts, and result size (off by default) |
| `BROWSER_MCP_PERF_LOG` | JSONL sink for perf records (default: `{workspace}/browser/perf/tools.jsonl`) |
| `BROWSER_MCP_VIEWPORT_DEFAULT` | `1` (default) makes `browser_get_elements` viewport-only; `0`/`false` restores a full-page tree |
| `BROWSER_MCP_OBSERVE_DEFAULT` | Default post-action observation (`diff`, `full`, or `none`; default `diff`). `browser_goto` still defaults to `full`. |
| `BROWSER_MCP_SETTLE_MS` | Max wait after an action before snapshotting (default 1500) |
| `BROWSER_MCP_QUIET_MS` | DOM-quiet window for post-action settle (default 150) |
| `BROWSER_MCP_FILL_MODE` | Default fill mode for `browser_input_by_index` (`auto`, `keys`, or `fast`) |
| `BROWSER_MCP_MAX_TABS` | Cap on agent-controlled tabs (default 5) |

All `BU_*` vars are passed through to `browser-harness` unchanged.

Playbooks are intentionally per-instance cache data. On Cloud Run, Fargate, and
other ephemeral-workspace targets they reset when the instance is recycled. Use
the memory storage contract for durable knowledge rather than adding a workspace
synchronization layer.

## Tools

Navigation, interaction, screenshots, tabs, waits, playbooks (`browser_list_playbooks`, `browser_read_playbook`, `browser_write_playbook`), `browser_login` for Spaces-saved passwords (returns `{ok, loggedIn, origin}` — never the password), and `browser_stop` for daemon cleanup.

### `browser_login` targets the focused tab

A sealed login runs in the tab the **user** has focused, because typing the credential requires Spaces to detach CDP from that tab first. Ordinary `browser_*` calls address tabs by CDP session id, so after `browser_switch_tab` — or if the user clicks another tab — the tab the agent is driving and the tab that receives the password are different ones.

Pass `expected_origin` to make the bridge refuse (`focused tab is on a different origin`) instead of signing in somewhere unintended, and check the returned `origin`. A saved credential is scoped to its own origin and must be marked for agent use, so a mismatch can never disclose another site's password — but it can still submit a login the user did not ask for.

A Spaces build predating `expectedOrigin` support ignores it and echoes no `origin`. Since that login cannot be verified after the fact, `browser_login` reports `in-app browser could not verify the origin` rather than a confirmed success; update Spaces to get the check.

Errors raised by browser tools are scrubbed of `?token=` values before they reach the agent: the in-app CDP token grants full control of the user's browser, and browser-harness echoes its endpoint in daemon log tails.

### Default: indexed DOM interaction (no screenshots needed)

`browser_get_elements`, `browser_click_by_index`, `browser_input_by_index`, and `browser_select_by_index` give the agent a **text-based, indexed** view of the page instead of screenshot + pixel coordinates — ported from [alibaba/page-agent](https://github.com/alibaba/page-agent)'s DOM-extraction engine (MIT licensed; see `src/browser_mcp/dom_indexing.py` and `src/browser_mcp/assets/` for provenance).

Workflow:

1. `browser_get_elements()` — returns an indexed text tree of the **viewport** by default (footer reports how many interactive elements are below; pass `viewport_only=false` or scroll). Filter with `kind=` (`inputs` / `buttons` / `links`) or `contains=`; `max_elements` defaults to 150. `observe="diff"` returns `{added, removed, unchanged}` vs the last tree for that tab (full tree after navigation or with no cache). Example:
   ```
   [12]<input placeholder='Email' />
   [35]<button aria-label='Submit form'>Submit</button>
   ```
2. `browser_click_by_index(35)` / `browser_input_by_index(12, "user@example.com")` / `browser_select_by_index(index, "Option text")` — each action waits for the DOM to settle and returns `{ok, action, page, observation}` plus the legacy top-level keys (`clicked`, `index`, `tagName`, `selected`). Default `observation` is a `diff` vs the last tree (`BROWSER_MCP_OBSERVE_DEFAULT`; pass `observe="full"` or `observe="none"`).
3. Read the observation in the action response. Only call `browser_get_elements` again when you need a different filter, the whole tree, or after navigation if the snapshot is not enough. Use `browser_get_text` to read page copy (`<main>` / `<article>` / `[role=main]`, else `body`; strips nav/footer/aside/script/style) instead of `browser_js("document.body.innerText")`.

`browser_goto(url)` navigates the current tab in place and returns a **full** observation by default (new document — no meaningful diff); pass `new_tab=True` to open a second tab (focused), or `tab=` to navigate a specific tab without focusing it. `browser_open_tab(..., focus=True)` also includes a full observation. `browser_input_by_index` defaults to an in-page fill (`mode="auto"`) and falls back to real key events when the framework reverts the value; pass `mode="keys"` for comboboxes and fields that only listen to `keydown` (or set `BROWSER_MCP_FILL_MODE=keys`). `browser_click_by_index` still clicks when another element covers the target and includes `"warning": "target obscured by <tag>"`. A huge diff (`added+removed` over 60 % of the tree) is replaced with `mode: "full"`. Pages that never go quiet hit `BROWSER_MCP_SETTLE_MS` and report `settled: false`.

### Tabs

The server owns a tab registry with short aliases (`t1`, `t2`, …, or a name from `browser_open_tab(alias=...)`). Every interaction tool accepts `tab=` as its last parameter; omitted means the focused tab (same behavior as before).

- **Reads never move focus:** `browser_get_elements`, `browser_get_text`, `browser_page_info`, `browser_js`, `browser_wait_for`, `browser_read_tabs`.
- **Actions focus first:** click/input/select/fill/press/scroll/upload/screenshot. Headed Chrome throttles timers and pauses painting in background tabs, so clicking or capturing there is unreliable.
- `browser_open_tab(url, alias=None, focus=False)` opens in the background by default. `browser_close_tab(tab)` refuses to close the last tab (navigates it to `about:blank` instead) and, if it was focused, focuses the most recently used remaining tab.
- `browser_tabs()` returns `{ok, focused, tabs: [{tab, alias, url, title, focused, opened_by_agent, last_used}, ...]}` with the focused tab first. `browser_switch_tab(target_id)` accepts aliases or raw target ids.
- `browser_read_tabs(tabs=None, mode="text", max_chars=3000)` fans out over agent-opened tabs (or the listed aliases) without changing focus.
- Cap: at most `BROWSER_MCP_MAX_TABS` (default 5) **agent-controlled** tabs (opened or addressed by the agent). User-opened tabs the agent never touched do not count. Hitting the cap returns `{ok: false, error: "tab_limit_reached", limit, tabs, action_required}` — the agent must ask the user which to close, then `browser_close_tab`, then retry. Nothing auto-closes a tab except `browser_stop` (agent-opened tabs) or an explicit `browser_close_tab`.
- `browser_wait_idle` on a non-focused tab falls back to DOM settle and returns `idle: null` with a note. Network idle stays on the focused tab only.
- In-app Spaces may not support `Target.createTarget`; that surfaces as `{ok: false, error: "this browser backend supports a single tab"}`.
- `browser_login` still targets the tab the **user** has focused, not `tab=`.

This is the default, preferred workflow: no image tokens, no coordinate-guessing, and clicks are resilient to layout shifts since they resolve through the live DOM rather than a fixed pixel position.

### Waits

`browser_wait_for(selector, visible=False, timeout=10)` waits in-page with a `MutationObserver` (one harness call for waits under 4s; longer timeouts are split to stay under the 5s IPC read timeout) instead of polling every 300 ms. `browser_wait_idle` waits for network idle on the focused tab, then for the DOM to go quiet (`settle`). On a background tab, or a backend without network events, it settle-only and returns `"idle": null` with a note. Prefer these over `browser_js` polling loops.

### Fallback: screenshots + coordinates

`browser_screenshot` is a **last-resort fallback**, for cases `browser_get_elements` can't handle: canvas-based apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming layout/rendering. It saves a **JPEG** (quality 60, `max_dim=1200`) under **`workspace/browser/Screenshots/`** and returns JSON with a workspace-relative `path` (for `load_file` on vision models), `bytes` (file size), `format`, `screenshots_dir`, url, title, and viewport — not inline image bytes. Pass `format="png"` for a PNG. `annotate=True` draws the current indexed-element labels onto the image so the next click can still use `browser_click_by_index`; `browser_click(x, y)` remains last-resort after that. This keeps tool results small for text-only models (Ollama) and avoids context-window blowups. Text-only models should use `browser_get_elements` (or `browser_js` for ad hoc extraction) instead of screenshots entirely, since they can't view images.

`browser-harness` is imported lazily on first browser tool call so listing MCP tools does not require Chrome to be running.

---

## Troubleshooting

```bash
uv run browser-harness --doctor
```

Common issues:

- **403 / permission-blocked** — on desktop Chrome, use `BU_CDP_URL` with a dedicated automation Chrome instead of the inspect checkbox. On Spaces, this is the in-app browser, not Google Chrome: there is no Allow-remote-debugging popup. Open the Browser panel and retry; a leftover token file is not enough — the published `in-app-cdp-url` file must be present.
- **DevToolsActivePort not found** — Chrome not running or wrong `BU_CDP_URL` / `BU_CDP_WS`.
- **MCP connects but navigation fails** — verify CDP endpoint from the same host/network namespace as the MCP subprocess.
