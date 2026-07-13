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

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `BU_CDP_URL` | HTTP DevTools URL → resolved to WebSocket (self-hosted headless) |
| `BU_CDP_WS` | Direct CDP WebSocket (cloud or custom) |
| `BU_NAME` | Daemon name; separate values for parallel browser sessions |
| `BROWSER_USE_API_KEY` | Browser Use Cloud auth |
| `BROWSER_MCP_PLAYBOOKS_DIR` | Playbooks directory (agent-written site notes) |
| `BROWSER_MCP_SCREENSHOTS_DIR` | Screenshot output directory (default: `{workspace}/browser/Screenshots`) |
| `BROWSER_MCP_SCREENSHOTS_MAX_FILES` | Retain at most this many PNGs (default `200`; `0` disables the cap) |
| `BROWSER_MCP_SCREENSHOTS_MAX_BYTES` | Retain at most this many total screenshot bytes (default `104857600`; `0` disables the cap) |

All `BU_*` vars are passed through to `browser-harness` unchanged.

Playbooks are intentionally per-instance cache data. On Cloud Run, Fargate, and
other ephemeral-workspace targets they reset when the instance is recycled. Use
the memory storage contract for durable knowledge rather than adding a workspace
synchronization layer.

For Cloud Run and other in-memory filesystems, set both screenshot caps to a
small value appropriate to the service memory limit.

---

## Tools

Navigation, interaction, screenshots, tabs, waits, playbooks (`browser_list_playbooks`, `browser_read_playbook`, `browser_write_playbook`), and `browser_stop` for daemon cleanup.

### Default: indexed DOM interaction (no screenshots needed)

`browser_get_elements`, `browser_click_by_index`, `browser_input_by_index`, and `browser_select_by_index` give the agent a **text-based, indexed** view of the page instead of screenshot + pixel coordinates — ported from [alibaba/page-agent](https://github.com/alibaba/page-agent)'s DOM-extraction engine (MIT licensed; see `src/browser_mcp/dom_indexing.py` and `src/browser_mcp/assets/` for provenance).

Workflow:

1. `browser_get_elements()` — returns an indexed text tree, e.g.:
   ```
   [12]<input placeholder='Email' />
   [35]<button aria-label='Submit form'>Submit</button>
   ```
2. `browser_click_by_index(35)` / `browser_input_by_index(12, "user@example.com")` / `browser_select_by_index(index, "Option text")`
3. Call `browser_get_elements()` again after navigation or any action that may have changed the DOM — indices are only valid for the tree they came from.

This is the default, preferred workflow: no image tokens, no coordinate-guessing, and clicks are resilient to layout shifts since they resolve through the live DOM rather than a fixed pixel position.

### Fallback: screenshots + coordinates

`browser_screenshot` + `browser_click(x, y)` is a **last-resort fallback**, for cases `browser_get_elements` can't handle: canvas-based apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming layout/rendering. `browser_screenshot` saves a PNG under **`workspace/browser/Screenshots/`** and returns JSON with a workspace-relative `path` (for `render_image` on vision models), `screenshots_dir`, url, title, and viewport — not inline base64 image bytes. This keeps tool results small for text-only models (Ollama) and avoids context-window blowups. Text-only models should use `browser_get_elements` (or `browser_js` for ad hoc extraction) instead of screenshots entirely, since they can't view images.

`browser-harness` is imported lazily on first browser tool call so listing MCP tools does not require Chrome to be running.

---

## Troubleshooting

```bash
uv run browser-harness --doctor
```

Common issues:

- **403 / permission-blocked** — use `BU_CDP_URL` with a dedicated automation Chrome instead of desktop Chrome with the inspect checkbox.
- **DevToolsActivePort not found** — Chrome not running or wrong `BU_CDP_URL` / `BU_CDP_WS`.
- **MCP connects but navigation fails** — verify CDP endpoint from the same host/network namespace as the MCP subprocess.
