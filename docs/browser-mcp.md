# Browser MCP

Stdio MCP server for MonkeyBot that wraps [browser-harness](https://github.com/browser-use/browser-harness) for real-browser control (CDP) and agent-writable site playbooks.

Package location: `integrations/browser-mcp/`.

---

## Install

```bash
uv sync --project integrations/browser-mcp
```

---

## Wire into MonkeyBot

Add to `monkeybot_config/mcp.json`:

```json
"browser": {
  "enabled": true,
  "command": "uv",
  "args": ["run", "--project", "../integrations/browser-mcp", "python", "-m", "browser_mcp.server"],
  "env": {
    "BU_NAME": "monkeybot",
    "BROWSER_MCP_PLAYBOOKS_DIR": "./workspace/skills/browser/playbooks",
    "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
  }
}
```

Tools appear as `browser__*` (MCP server name + tool name). Copy `demo_agent/workspace/skills/browser/` into your `SKILLS_PATH` so the agent gets workflow instructions and a playbooks directory. See also [Skills](skills.md).

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
  "BROWSER_MCP_PLAYBOOKS_DIR": "./workspace/skills/browser/playbooks",
  "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
}
```

**Container pattern:** start Chromium in the same pod (sidecar or entrypoint) before the gateway so the MCP process can reach `127.0.0.1:9222`. Install `google-chrome-stable` or `chromium` in the image; avoid Snap-packaged Chromium on Linux (CDP binding issues — see [browser-harness snap docs](https://github.com/browser-use/browser-harness/blob/main/docs/snap-linux-headless.md)).

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
  "BROWSER_MCP_PLAYBOOKS_DIR": "./workspace/skills/browser/playbooks",
  "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
}
```

Call the `browser_stop` MCP tool when browsing is done — cloud sessions bill until stopped. As a safety net, MonkeyBot's `MCPClient.disconnect()`/`disconnect_all()` also call `browser_stop` automatically (best-effort, 10s timeout) whenever a connected server exposes that tool, so agent crashes, abandoned conversations, and normal shutdown all stop the remote session rather than only killing the local stdio subprocess.

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

All `BU_*` vars are passed through to `browser-harness` unchanged.

---

## Tools

Navigation, interaction, screenshots, tabs, waits, playbooks (`browser_list_playbooks`, `browser_read_playbook`, `browser_write_playbook`), and `browser_stop` for daemon cleanup.

`browser_screenshot` saves a PNG under **`./browser/Screenshots/`** in the agent workspace and returns JSON with a workspace-relative `path` (for `render_image` on vision models), `screenshots_dir`, url, title, and viewport — not inline base64 image bytes. This keeps tool results small for text-only models (Ollama) and avoids context-window blowups. Use `browser_js` to extract visible page text when the model cannot view images.

`browser-harness` is imported lazily on first browser tool call so listing MCP tools does not require Chrome to be running.

---

## Troubleshooting

```bash
uv run --project integrations/browser-mcp browser-harness --doctor
```

Common issues:

- **403 / permission-blocked** — use `BU_CDP_URL` with a dedicated automation Chrome instead of desktop Chrome with the inspect checkbox.
- **DevToolsActivePort not found** — Chrome not running or wrong `BU_CDP_URL` / `BU_CDP_WS`.
- **MCP connects but navigation fails** — verify CDP endpoint from the same host/network namespace as the MCP subprocess.
