"""Stdio MCP server: browser-harness tools + agent-writable site playbooks."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from browser_mcp import playbooks

logger = logging.getLogger(__name__)

_bh: tuple[Any, Any] | None = None

mcp = FastMCP(
    "browser",
    instructions=(
        "Real-browser control via CDP (browser-harness). Use browser_* tools for web tasks. "
        "Check browser_list_playbooks / browser_read_playbook before improvising on a site; "
        "call browser_write_playbook after learning non-obvious flows. "
        "Call browser_stop when done with remote/cloud sessions."
    ),
)


def _browser_harness() -> tuple[Any, Any]:
    """Lazy import + daemon bootstrap on first browser tool use."""
    global _bh
    if _bh is None:
        from browser_harness import admin, helpers

        admin.ensure_daemon()
        _bh = (helpers, admin)
    return _bh


def _json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@mcp.tool()
def browser_goto(url: str) -> str:
    """Navigate to a URL in the browser (opens or reuses a tab). Returns page info and matching playbook filenames."""
    helpers, _ = _browser_harness()
    helpers.new_tab(url)
    helpers.wait_for_load()
    info = helpers.page_info()
    names = playbooks.list_playbook_names(url)
    return _json_text({**info, "playbooks": names})


@mcp.tool()
def browser_screenshot(full: bool = False, max_dim: int | None = 1800) -> str:
    """Capture a PNG of the current viewport. Returns JSON with host path and page metadata (not inline image bytes)."""
    helpers, _ = _browser_harness()
    path = helpers.capture_screenshot(full=full, max_dim=max_dim)
    info = helpers.page_info()
    return _json_text(
        {
            "ok": True,
            "path": path,
            "url": info.get("url"),
            "title": info.get("title"),
            "viewport": {"w": info.get("w"), "h": info.get("h")},
            "note": (
                "Screenshot saved on the gateway host. Use browser_js to extract visible text "
                "when the model cannot view images. Coordinate clicks still use this capture."
            ),
        }
    )


@mcp.tool()
def browser_click(x: float, y: float, button: str = "left", clicks: int = 1) -> str:
    """Click at viewport coordinates (x, y). Prefer screenshot + coordinates for complex UIs."""
    helpers, _ = _browser_harness()
    helpers.click_at_xy(x, y, button=button, clicks=clicks)
    return _json_text({"ok": True})


@mcp.tool()
def browser_fill(selector: str, text: str, clear_first: bool = True, timeout: float = 0.0) -> str:
    """Fill a form input (works with React/Vue controlled inputs)."""
    helpers, _ = _browser_harness()
    helpers.fill_input(selector, text, clear_first=clear_first, timeout=timeout)
    return _json_text({"ok": True})


@mcp.tool()
def browser_press_key(key: str, modifiers: int = 0) -> str:
    """Press a key. Modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift."""
    helpers, _ = _browser_harness()
    helpers.press_key(key, modifiers=modifiers)
    return _json_text({"ok": True})


@mcp.tool()
def browser_scroll(x: float, y: float, dy: float = -300, dx: float = 0) -> str:
    """Scroll the page at viewport position (x, y)."""
    helpers, _ = _browser_harness()
    helpers.scroll(x, y, dy=dy, dx=dx)
    return _json_text({"ok": True})


@mcp.tool()
def browser_js(expression: str) -> str:
    """Evaluate JavaScript in the attached tab and return the result (DOM read/extraction)."""
    helpers, _ = _browser_harness()
    result = helpers.js(expression)
    return _json_text({"ok": True, "result": result})


@mcp.tool()
def browser_wait_for(selector: str, visible: bool = False, timeout: float = 10.0) -> str:
    """Wait until an element matching selector exists (optionally visible)."""
    helpers, _ = _browser_harness()
    found = helpers.wait_for_element(selector, timeout=timeout, visible=visible)
    return _json_text({"ok": found, "found": found})


@mcp.tool()
def browser_wait_idle(timeout: float = 10.0, idle_ms: float = 500) -> str:
    """Wait until network activity is idle (useful after SPA navigation or form submit)."""
    helpers, _ = _browser_harness()
    idle = helpers.wait_for_network_idle(timeout=timeout, idle_ms=idle_ms)
    return _json_text({"ok": idle, "idle": idle})


@mcp.tool()
def browser_page_info() -> str:
    """Return current page url, title, viewport size, and scroll position."""
    helpers, _ = _browser_harness()
    return _json_text(helpers.page_info())


@mcp.tool()
def browser_tabs() -> str:
    """List open browser tabs."""
    helpers, _ = _browser_harness()
    return _json_text(helpers.list_tabs(include_chrome=False))


@mcp.tool()
def browser_switch_tab(target_id: str) -> str:
    """Switch to a tab by target_id (from browser_tabs)."""
    helpers, _ = _browser_harness()
    sid = helpers.switch_tab(target_id)
    return _json_text({"ok": True, "session_id": sid})


@mcp.tool()
def browser_upload(selector: str, path: str) -> str:
    """Set files on a file input. path must be an absolute filepath on the host."""
    helpers, _ = _browser_harness()
    helpers.upload_file(selector, path)
    return _json_text({"ok": True})


@mcp.tool()
def browser_list_playbooks(host: str | None = None) -> str:
    """List playbook markdown filenames, optionally filtered by host or URL."""
    return _json_text(
        {
            "ok": True,
            "playbooks_dir": str(playbooks.playbooks_dir()),
            "playbooks": playbooks.list_playbook_names(host),
        }
    )


@mcp.tool()
def browser_read_playbook(host: str) -> str:
    """Read the playbook markdown for a host or URL."""
    try:
        content = playbooks.read_playbook(host)
    except playbooks.PlaybookError as exc:
        logger.warning("browser_read_playbook failed for host=%r: %s", host, exc)
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text({"ok": True, "host": playbooks.host_slug(host), "content": content})


@mcp.tool()
def browser_write_playbook(host: str, content: str, append: bool = False) -> str:
    """Write or append a site playbook under the playbooks directory only (host slug filename)."""
    try:
        result = playbooks.write_playbook(host, content, append=append)
    except playbooks.PlaybookError as exc:
        logger.warning("browser_write_playbook failed for host=%r: %s", host, exc)
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text(result)


@mcp.tool()
def browser_stop() -> str:
    """Stop the browser-harness daemon (cleanup after browsing; important for cloud browsers)."""
    from browser_harness import admin

    admin.restart_daemon()
    global _bh
    _bh = None
    return _json_text({"ok": True, "message": "daemon stopped"})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
