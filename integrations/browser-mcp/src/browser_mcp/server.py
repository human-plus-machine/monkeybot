"""Stdio MCP server: browser-harness tools + agent-writable site playbooks."""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from browser_mcp import dom_indexing, playbooks, screenshots

logger = logging.getLogger(__name__)

_bh: tuple[Any, Any] | None = None
# CDP endpoint (BU_CDP_URL/WS or in-app file) the current daemon binding was ensured with.
# Used instead of browser-harness's nonexistent daemon_browser_kind() to decide when to bounce.
_bound_cdp: str | None = None
# True when the last _apply_in_app_cdp_url() call set BU_CDP_URL/WS from the in-app file
# (as opposed to an operator-supplied env var). Lets us clear that self-set value when the
# file goes away, instead of falling back to a port we wrote from a now-stale file read.
_env_set_from_in_app_file = False

# Written by Monkeyapp when the in-app Electron CDP bridge is live. Prefer this over
# auto-discovering the user's desktop Chrome (which wins when BU_CDP_URL is empty/stale).
_IN_APP_CDP_URL_FILE = Path.home() / ".monkeybot" / "runtime" / "in-app-cdp-url"


mcp = FastMCP(
    "browser",
    instructions=(
        "Real-browser control via CDP (browser-harness). Use browser_* tools for web tasks.\n"
        "\n"
        "Default workflow — text-based, indexed DOM interaction:\n"
        "1. browser_get_elements() to see interactive elements as an indexed text tree "
        "(no image tokens, no pixel-guessing).\n"
        "2. browser_click_by_index(index) / browser_input_by_index(index, text) / "
        "browser_select_by_index(index, text) to act on them.\n"
        "3. Call browser_get_elements() again after any action that may have changed the "
        "page (navigation, dynamic content, modal opened) — indices are only valid for the "
        "tree they came from.\n"
        "\n"
        "browser_screenshot + browser_click(x, y) is a LAST-RESORT FALLBACK only — use it "
        "when browser_get_elements returns nothing useful for what you need (canvas apps, "
        "heavily shadow-DOM UIs, drag-and-drop, or visually confirming layout/rendering). "
        "Do not default to screenshots for ordinary clicking/typing.\n"
        "\n"
        "Check browser_list_playbooks / browser_read_playbook before improvising on a site; "
        "call browser_write_playbook after learning non-obvious flows. "
        "Call browser_stop when done with remote/cloud sessions."
    ),
)


def _read_in_app_cdp_file() -> str | None:
    try:
        raw = _IN_APP_CDP_URL_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "browser-mcp: failed reading in-app CDP URL file %s",
            _IN_APP_CDP_URL_FILE,
            exc_info=True,
        )
        return None
    return raw or None


def _apply_in_app_cdp_url() -> str | None:
    """Ensure BU_CDP_URL/WS points at Monkeyapp's bridge when one is published.

    Prefers the in-app runtime file over process env: mcp.json often bakes a
    concrete ``http://127.0.0.1:PORT`` from a previous launch, and that port is
    dead after restart (WinError 10061 / connection refused). The file is
    rewritten every time Electron's CDP bridge comes up.

    When the file disappears (Monkeyapp closed) after having supplied the
    active endpoint, the env var we set from it is cleared rather than left in
    place -- otherwise the next call would fall back to that same self-set,
    now-dead port, reintroducing the stale-endpoint bug this function exists
    to avoid. Operator-supplied env vars (self-hosted headless Chrome, Browser
    Use Cloud -- see docs/browser-mcp.md) are never touched by this path.

    Returns the explicit CDP endpoint in use (file or env), or None.
    """
    global _env_set_from_in_app_file

    file_url = _read_in_app_cdp_file()
    if file_url:
        if file_url.startswith("ws://") or file_url.startswith("wss://"):
            os.environ["BU_CDP_WS"] = file_url
            os.environ.pop("BU_CDP_URL", None)
            _env_set_from_in_app_file = True
            return file_url
        if file_url.startswith("http://") or file_url.startswith("https://"):
            os.environ["BU_CDP_URL"] = file_url
            os.environ.pop("BU_CDP_WS", None)
            _env_set_from_in_app_file = True
            return file_url

    if _env_set_from_in_app_file:
        os.environ.pop("BU_CDP_WS", None)
        os.environ.pop("BU_CDP_URL", None)
        _env_set_from_in_app_file = False

    ws = (os.environ.get("BU_CDP_WS") or "").strip()
    http = (os.environ.get("BU_CDP_URL") or "").strip()
    if ws:
        return ws
    if http:
        return http
    return None


def _browser_harness() -> tuple[Any, Any]:
    """Lazy import + daemon bootstrap on first browser tool use.

    When an explicit CDP URL is configured (env or Monkeyapp runtime file) and the
    live daemon was bound to a different endpoint (or none — i.e. local Chrome),
    bounce it so tool calls drive the in-app panel instead.
    """
    global _bh, _bound_cdp
    cdp = _apply_in_app_cdp_url()
    if _bh is not None and cdp == _bound_cdp:
        return _bh

    from browser_harness import admin, helpers

    # Fresh process: _bound_cdp is None while an external local-Chrome daemon may
    # still be alive. Restart whenever the desired CDP differs from what we last
    # ensured (browser-harness 0.1.3 has no daemon_browser_kind() to query).
    if admin.daemon_alive() and _bound_cdp != cdp:
        logger.info(
            "browser-mcp: replacing harness daemon for CDP %s (was %s)",
            cdp,
            _bound_cdp,
        )
        admin.restart_daemon()
        _bh = None

    admin.ensure_daemon()
    _bh = (helpers, admin)
    _bound_cdp = cdp
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
def browser_get_elements(viewport_only: bool = False) -> str:
    """Return interactive elements as an indexed text tree — the default way to see the page.

    Prefer this over browser_screenshot for locating things to click/type into. Injects a
    DOM-walker (vendored from alibaba/page-agent, MIT licensed) that scans the live DOM and
    returns lines like ``[12]<input placeholder='Email' />`` / ``[35]<button>Submit</button>``,
    indentation shows parent/child nesting. Use the numeric index with browser_click_by_index /
    browser_input_by_index / browser_select_by_index — no coordinates needed.

    Set viewport_only=True to restrict to the visible viewport (default scans the full page).
    Re-call this after navigation or any action that may have changed the DOM — indices are
    only valid for the tree they came from.
    """
    helpers, _ = _browser_harness()
    result = dom_indexing.get_elements(helpers, viewport_only)
    return _json_text({"ok": True, **result})


@mcp.tool()
def browser_click_by_index(index: int) -> str:
    """Click an interactive element by index from browser_get_elements. Prefer this over
    browser_click(x, y) — no pixel-guessing, resilient to layout shifts."""
    helpers, _ = _browser_harness()
    try:
        rect = dom_indexing.get_rect(helpers, index)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    helpers.click_at_xy(rect["x"], rect["y"])
    return _json_text({"ok": True, "clicked": rect})


@mcp.tool()
def browser_input_by_index(index: int, text: str, clear_first: bool = True) -> str:
    """Click and type text into an indexed input/textarea/contenteditable element from
    browser_get_elements. Prefer this over screenshot + coordinate typing."""
    helpers, _ = _browser_harness()
    try:
        info = dom_indexing.get_input_info(helpers, index)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    # fill_input drives real key events through framework-controlled inputs (React/Vue),
    # matching browser_fill's behavior instead of a hand-rolled clear+type loop here.
    helpers.fill_input(info["selector"], text, clear_first=clear_first)
    return _json_text({"ok": True, "index": index, "tagName": info.get("tagName")})


@mcp.tool()
def browser_select_by_index(index: int, text: str) -> str:
    """Select a <select> dropdown option by visible text, using the index from
    browser_get_elements."""
    helpers, _ = _browser_harness()
    try:
        dom_indexing.select_option(helpers, index, text)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text({"ok": True, "index": index, "selected": text})


@mcp.tool()
def browser_screenshot(full: bool = False, max_dim: int | None = 1800) -> str:
    """Capture a PNG of the current viewport.

    LAST-RESORT FALLBACK: use browser_get_elements + browser_click_by_index /
    browser_input_by_index for ordinary clicking and typing instead — it's cheaper (no
    image tokens) and more reliable (indexed elements vs. guessed pixels). Reach for
    this tool only when browser_get_elements doesn't surface what you need: canvas-based
    apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming rendering/layout.

    Returns JSON with a workspace-relative path under ``./browser/Screenshots/`` (for
    ``load_file`` on vision models) plus page metadata — not inline image bytes.
    """
    helpers, _ = _browser_harness()
    abs_path, rel_path = screenshots.allocate_screenshot_path()
    helpers.capture_screenshot(path=str(abs_path), full=full, max_dim=max_dim)
    info = helpers.page_info()
    return _json_text(
        {
            "ok": True,
            "path": rel_path,
            "screenshots_dir": "./browser/Screenshots",
            "url": info.get("url"),
            "title": info.get("title"),
            "viewport": {"w": info.get("w"), "h": info.get("h")},
            "note": (
                "Screenshot saved under the agent workspace. Vision models: call load_file "
                "with path. Text-only models: use browser_get_elements instead of this tool. "
                "Coordinate clicks use viewport metadata from this capture."
            ),
        }
    )


@mcp.tool()
def browser_click(x: float, y: float, button: str = "left", clicks: int = 1) -> str:
    """Click at viewport coordinates (x, y).

    LAST-RESORT FALLBACK: prefer browser_get_elements + browser_click_by_index for
    ordinary clicking. Only use raw coordinates for elements browser_get_elements can't
    represent (canvas, shadow DOM, drag targets) or after visually confirming a spot via
    browser_screenshot.
    """
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
    global _bh, _bound_cdp
    _bh = None
    _bound_cdp = None
    return _json_text({"ok": True, "message": "daemon stopped"})


def _stop_daemon_for_shutdown() -> None:
    """Best-effort daemon stop on process exit (SIGTERM/SIGINT/atexit).

    A crashed turn, abandoned conversation, or container SIGTERM can end this
    stdio process without the model ever calling ``browser_stop``. Closing this
    process's own stdio pipes never reaches the detached browser-harness daemon
    (started via ``start_new_session=True``), so without this hook a remote
    Browser Use Cloud session -- and its billing -- would keep running
    indefinitely. Idempotent: safe to call even if no daemon was ever started.
    """
    global _bh, _bound_cdp
    if _bh is None:
        return
    _bh = None
    _bound_cdp = None
    try:
        from browser_harness import admin

        admin.restart_daemon()
    except Exception:
        logger.warning("browser-mcp shutdown: failed to stop browser-harness daemon", exc_info=True)


def _install_shutdown_handlers() -> None:
    """Register atexit + SIGTERM/SIGINT hooks so daemon teardown survives process exit.

    SIGKILL cannot be intercepted by any process (OS-level guarantee) -- this
    only closes the gap for SIGTERM, which orchestrators (Kubernetes, Docker,
    ECS) send first with a grace period before escalating to SIGKILL.
    """
    atexit.register(_stop_daemon_for_shutdown)

    def _handle_signal(signum: int, _frame: object) -> None:
        _stop_daemon_for_shutdown()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Not the main thread, or unsupported on this platform -- atexit still
        # covers normal interpreter shutdown in that case.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handle_signal)


def main() -> None:
    _install_shutdown_handlers()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
