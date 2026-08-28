"""Stdio MCP server: browser-harness tools + agent-writable site playbooks."""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from mcp.server.fastmcp import FastMCP

from browser_mcp import agentcore, dom_indexing, playbooks, screenshots

logger = logging.getLogger(__name__)

_bh: tuple[Any, Any] | None = None
# CDP endpoint (BU_CDP_URL/WS or in-app file) the current daemon binding was ensured with,
# or the literal "agentcore" when bound to the AgentCore backend.
# Used instead of browser-harness's nonexistent daemon_browser_kind() to decide when to bounce.
_bound_cdp: str | None = None
_agentcore_admin: agentcore.AgentCoreAdmin | None = None
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
        "If the user asked to sign in on the Spaces in-app browser and a saved "
        "password exists, call browser_login() — never read or type the password "
        "yourself. "
        "Call browser_stop when done with remote/cloud sessions."
    ),
)


# Matches monkeyapp BROWSER_TARGET_ID ('monkeybot'). The Spaces upgrade handler
# accepts any /devtools/browser/* path, so this only has to stay in sync for the
# URL we advertise, not for routing.
_IN_APP_BROWSER_WS_PATH = "/devtools/browser/monkeybot"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOGIN_PUBLIC_ERRORS = frozenset(
    {
        "in-app browser is not available",
        "in-app browser token is missing",
        "no tab",
        "not a web page",
        "no saved password for this site",
        "this password is not allowed for agent use",
        "unexpected login response",
        "login failed",
    }
)
_IN_APP_CDP_REJECTED = (
    "in-app browser CDP rejected the connection. This is the Spaces browser, "
    "not Google Chrome — there is no Allow-remote-debugging popup to click. "
    "Open the Browser panel in Spaces and retry."
)
# Passing ProxyHandler({}) makes urllib skip its default env-based proxy
# handler. The empty handler itself is then dropped (no *_open methods),
# so loopback requests never honor HTTP_PROXY.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOGIN_TIMEOUT_S = 90
_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s]*", re.IGNORECASE)


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


def _read_in_app_cdp_token() -> str | None:
    token_file = _IN_APP_CDP_URL_FILE.parent / "in-app-cdp-token"
    try:
        raw = token_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "browser-mcp: failed reading in-app CDP token file %s",
            token_file,
            exc_info=True,
        )
        return None
    return raw or None


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    return host.lower().strip("[]") in _LOOPBACK_HOSTS


def _query_token(query: str) -> str | None:
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == "token":
            return value or None
    return None


def _with_query_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "token"
    ]
    pairs.append(("token", token))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _in_app_ws_url(url: str, token: str | None) -> str:
    """browser-harness treats HTTP 403 as Chrome's Allow-debugging popup.

    The in-app bridge requires a bearer/query token on every request, including
    ``/json/version``. Point the daemon at the tokenized WebSocket URL so it
    never does that unauthenticated HTTP probe. The local token is only ever
    attached to a loopback URL. File token wins over a query token already on
    the published URL.
    """
    parsed = urlparse(url)
    if not _is_loopback_host(parsed.hostname):
        return url
    attached = token or _query_token(parsed.query)
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{scheme}://{host}:{port}{_IN_APP_BROWSER_WS_PATH}"
    return _with_query_token(url, attached)


def _redact_cdp_token(message: str) -> str:
    """Strip query tokens so harness log tails cannot leak into agent transcripts."""
    return _TOKEN_QUERY_RE.sub(r"\1[redacted]", message)


def _bind_in_app_endpoint(url: str, token: str | None) -> str:
    global _env_set_from_in_app_file
    endpoint = _in_app_ws_url(url, token)
    os.environ["BU_CDP_WS"] = endpoint
    os.environ.pop("BU_CDP_URL", None)
    _env_set_from_in_app_file = True
    return endpoint


def _looks_like_in_app_cdp_rejection(message: str) -> bool:
    lower = message.lower()
    if "permission-blocked" in lower or "allow remote debugging" in lower:
        return True
    return "ws handshake failed" in lower and "403" in lower


def _raise_rewritten_in_app_cdp_error(exc: BaseException) -> None:
    """Replace Chrome-popup copy when a 403 actually came from Spaces.

    Only rewrite when the in-app URL file is published (or we just bound it).
    A leftover token file is not evidence the bridge is live.
    """
    if not _looks_like_in_app_cdp_rejection(str(exc)):
        return
    if not (_read_in_app_cdp_file() or _env_set_from_in_app_file):
        return
    # Drop the cause chain so a tokenized WS URL in the original message
    # cannot leak into the agent transcript.
    raise RuntimeError(_IN_APP_CDP_REJECTED) from None


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
    token = _read_in_app_cdp_token()
    if file_url and _is_loopback_host(urlparse(file_url).hostname):
        return _bind_in_app_endpoint(file_url, token)

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


def _teardown_bound_backend() -> None:
    """Tear down whatever backend _bh is currently bound to, if any.

    Clears ``_bh`` before attempting the (possibly failing) teardown call, so a
    raised exception here never leaves stale backend state behind for the next
    ``_browser_harness()`` call to mistakenly reuse. Dispatches on ``_bound_cdp``
    rather than introspecting ``_bh``'s admin object, since the non-agentcore
    path always re-imports (and stops) the real ``browser_harness.admin``
    module regardless of what's stored in ``_bh``.
    """
    global _bh
    if _bh is None:
        return
    _, admin = _bh
    is_agentcore = _bound_cdp == "agentcore"
    _bh = None
    if is_agentcore:
        admin.stop_session()
        from browser_mcp import playwright_helpers

        playwright_helpers.disconnect()
    else:
        from browser_harness import admin as bh_admin

        bh_admin.restart_daemon()


def _reconnect_agentcore() -> tuple[str, dict[str, str]]:
    """Force a fresh AgentCore session (stop + restart) and return new ws creds.

    Registered with playwright_helpers as its reconnect hook: a stale/expired
    AgentCore session (~15-30 min TTL) leaves the old ws connection dead, and
    plain ensure_session() would just re-sign headers for the same (already
    dead) session, so the old session is explicitly stopped first.
    """
    assert _agentcore_admin is not None
    _agentcore_admin.stop_session()
    return _agentcore_admin.ensure_session()


def _agentcore_browser_harness() -> tuple[Any, Any]:
    """Bind _bh to the AgentCore backend (StartBrowserSession + Playwright CDP connect)."""
    global _bh, _bound_cdp, _agentcore_admin
    from browser_mcp import playwright_helpers

    if _agentcore_admin is None:
        _agentcore_admin = agentcore.AgentCoreAdmin(agentcore.resolve_region())

    ws_url, headers = _agentcore_admin.ensure_session()
    playwright_helpers.connect(ws_url, headers)
    playwright_helpers.set_reconnect_hook(_reconnect_agentcore)
    _bh = (playwright_helpers, _agentcore_admin)
    _bound_cdp = "agentcore"
    return _bh


def _browser_harness() -> tuple[Any, Any]:
    """Lazy import + daemon bootstrap on first browser tool use.

    When an explicit CDP URL is configured (env or Monkeyapp runtime file) and the
    live daemon was bound to a different endpoint (or none — i.e. local Chrome),
    bounce it so tool calls drive the in-app panel instead. BROWSER_BACKEND=agentcore
    (with no explicit CDP endpoint) dispatches to AWS Bedrock AgentCore Browser instead.
    """
    global _bh, _bound_cdp
    cdp = _apply_in_app_cdp_url()

    if agentcore.agentcore_backend_requested():
        if _bh is not None and _bound_cdp == "agentcore":
            return _bh
        _teardown_bound_backend()
        return _agentcore_browser_harness()

    if _bh is not None and cdp == _bound_cdp:
        return _bh

    if _bound_cdp == "agentcore":
        _teardown_bound_backend()

    from browser_harness import admin, helpers

    # Fresh process: _bound_cdp is None while an external local-Chrome daemon may
    # still be alive. Restart whenever the desired CDP differs from what we last
    # ensured (browser-harness 0.1.3 has no daemon_browser_kind() to query).
    if admin.daemon_alive() and _bound_cdp != cdp:
        logger.info(
            "browser-mcp: replacing harness daemon for CDP %s (was %s)",
            _redact_cdp_token(str(cdp)) if cdp else cdp,
            _redact_cdp_token(str(_bound_cdp)) if _bound_cdp else _bound_cdp,
        )
        admin.restart_daemon()
        _bh = None

    try:
        admin.ensure_daemon()
    except Exception as exc:
        _raise_rewritten_in_app_cdp_error(exc)
        message = _redact_cdp_token(str(exc))
        if message != str(exc):
            raise RuntimeError(message) from None
        raise
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


def _format_loopback_host(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _in_app_http_origin(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        return None
    if not _is_loopback_host(parsed.hostname) or not parsed.hostname:
        return None
    http_scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
    default_port = 443 if http_scheme == "https" else 80
    port = parsed.port or default_port
    return f"{http_scheme}://{_format_loopback_host(parsed.hostname)}:{port}"


def _in_app_http_and_token() -> tuple[str | None, str | None]:
    """HTTP origin + bearer token for the published in-app CDP bridge only."""
    _apply_in_app_cdp_url()
    file_url = _read_in_app_cdp_file()
    if not file_url:
        return None, None
    http = _in_app_http_origin(file_url)
    if not http:
        return None, None
    token = _read_in_app_cdp_token() or _query_token(urlparse(file_url).query)
    return http, token


def _public_login_result(body: dict[str, Any]) -> dict[str, Any]:
    error = body.get("error")
    result: dict[str, Any] = {
        "ok": bool(body.get("ok")),
        "loggedIn": bool(body.get("loggedIn")),
    }
    if error:
        message = str(error)
        result["error"] = message if message in _LOGIN_PUBLIC_ERRORS else "login failed"
    return result


def _loopback_open(req: urllib.request.Request, timeout: float = _LOGIN_TIMEOUT_S) -> Any:
    return _LOOPBACK_OPENER.open(req, timeout=timeout)


def _sealed_login(username: str | None) -> dict[str, Any]:
    http, token = _in_app_http_and_token()
    if not http:
        return {"ok": False, "loggedIn": False, "error": "in-app browser is not available"}
    if not token:
        return {"ok": False, "loggedIn": False, "error": "in-app browser token is missing"}
    payload = json.dumps({"username": username} if username else {}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(f"{http}/json/login", data=payload, headers=headers, method="POST")
    try:
        with _loopback_open(req, timeout=_LOGIN_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            logger.warning("browser_login HTTP %s", getattr(exc, "code", "?"))
            return {"ok": False, "loggedIn": False, "error": "login failed"}
    except Exception:
        logger.warning("browser_login failed", exc_info=True)
        return {"ok": False, "loggedIn": False, "error": "login failed"}
    if not isinstance(body, dict):
        return {"ok": False, "loggedIn": False, "error": "unexpected login response"}
    return _public_login_result(body)


@mcp.tool()
def browser_login(username: str | None = None) -> str:
    """Log in with a saved Spaces password without revealing the credential.

    Call this only when the user asked to sign in. Do not type or read the
    password. Returns only {ok, loggedIn} — never the password value.
    """
    result = _sealed_login(username)
    return _json_text(result)


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


def _stop_active_backend_best_effort() -> None:
    """Stop whatever backend may be active, matching the pre-agentcore contract
    that browser_stop / shutdown always best-effort stop the browser-harness
    daemon -- even in a fresh process where ``_bh`` was never bound here, since
    an external/leftover daemon (e.g. a still-billing Browser Use Cloud session
    from a prior process) may still be alive (see ``_browser_harness()``'s
    "Fresh process" comment). AgentCore sessions are only ever started by this
    process, so those are only stopped when actually bound.
    """
    global _bh
    if _bound_cdp == "agentcore":
        _teardown_bound_backend()
        return
    _bh = None
    from browser_harness import admin

    admin.restart_daemon()


@mcp.tool()
def browser_stop() -> str:
    """Stop the active browser backend (cleanup after browsing; important for cloud/AgentCore browsers)."""
    global _bound_cdp
    try:
        _stop_active_backend_best_effort()
    except Exception as exc:
        _bound_cdp = None
        logger.warning("browser_stop: failed to stop browser backend", exc_info=True)
        return _json_text({"ok": False, "error": str(exc)})
    _bound_cdp = None
    return _json_text({"ok": True, "message": "browser backend stopped"})


def _stop_daemon_for_shutdown() -> None:
    """Best-effort backend stop on process exit (SIGTERM/SIGINT/atexit).

    A crashed turn, abandoned conversation, or container SIGTERM can end this
    stdio process without the model ever calling ``browser_stop``. Closing this
    process's own stdio pipes never reaches a detached browser-harness daemon
    (started via ``start_new_session=True``) or a live AgentCore session, so
    without this hook a remote Browser Use Cloud / AgentCore session -- and its
    billing -- would keep running indefinitely. Always attempts the stop, even
    if this process never itself bound a backend (see
    ``_stop_active_backend_best_effort``'s docstring). Idempotent.
    """
    global _bound_cdp
    try:
        _stop_active_backend_best_effort()
    except Exception:
        logger.warning("browser-mcp shutdown: failed to stop browser backend", exc_info=True)
    _bound_cdp = None


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
