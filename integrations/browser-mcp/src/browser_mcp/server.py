"""Stdio MCP server: browser-harness tools + agent-writable site playbooks."""

from __future__ import annotations

import atexit
import contextlib
import functools
import json
import logging
import os
import re
import signal
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, ParamSpec
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from mcp.server.fastmcp import FastMCP

from browser_mcp import agentcore, dom_indexing, perf, playbooks, screenshots, tabs

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
        "(viewport by default; no image tokens, no pixel-guessing). Use kind=/contains= "
        "to narrow, viewport_only=false or scroll when the footer says more are below, "
        "and browser_get_text for readable page text.\n"
        "2. browser_click_by_index(index) / browser_input_by_index(index, text) / "
        "browser_select_by_index(index, text) to act on them. Indices remain valid "
        "until navigation.\n"
        "3. After navigation call browser_get_elements() again. Pass observe=\"diff\" "
        "to see only added/removed lines when the DOM mutated in place.\n"
        "\n"
        "browser_screenshot is a LAST-RESORT FALLBACK only — use it when "
        "browser_get_elements returns nothing useful (canvas apps, heavily shadow-DOM "
        "UIs, drag-and-drop, or visually confirming layout/rendering). Prefer "
        "browser_screenshot(annotate=True) then browser_click_by_index when indices "
        "are available; browser_click(x, y) is last-resort after that. Do not default "
        "to screenshots for ordinary clicking/typing.\n"
        "\n"
        "Check browser_list_playbooks / browser_read_playbook before improvising on a site; "
        "call browser_write_playbook after learning non-obvious flows. "
        "If the user asked to sign in on the Spaces in-app browser and a saved "
        "password exists, call browser_login(expected_origin=...) — never read or "
        "type the password yourself, and check the returned origin. "
        "Call browser_stop when done with remote/cloud sessions.\n"
        "\n"
        "Tabs: each tab has a short alias (t1, t2, …) or a name you pass to "
        "browser_open_tab(alias=...). Reads (get_elements, get_text, page_info, js, wait_for, "
        "read_tabs) never move focus — pass tab= to address a background tab. "
        "Actions (click, input, select, fill, screenshot, …) focus the tab first "
        "because background tabs throttle timers and pause painting. Open a second "
        "tab to compare pages, keep a form while reading docs, or fan out with "
        "browser_read_tabs. At most five agent-controlled tabs; if you hit the cap, "
        "relay the returned tab list to the user, ask which to close, then "
        "browser_close_tab and retry — never close a tab without their confirmation. "
        "Close tabs you opened when done. Do not expect a background SPA to finish "
        "loading while unfocused."
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
        "in-app browser could not verify the origin",
        "no tab",
        "not a web page",
        "focused tab is on a different origin",
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
_P = ParamSpec("_P")
_TOOL_LOCK = threading.RLock()
_WAIT_IDLE_NOTE = (
    "network idle is only available on the focused tab; DOM settle was used"
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
    attached to a loopback URL. A query token already on the published URL
    wins over a leftover token file — Spaces writes the URL first.
    """
    parsed = urlparse(url)
    if not _is_loopback_host(parsed.hostname):
        return url
    attached = _query_token(parsed.query) or token
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


def _reraise_public_harness_error(exc: BaseException) -> NoReturn:
    """Rewrite in-app 403s and strip query tokens before they reach the agent.

    Call only from an ``except`` block. Unchanged messages are re-raised as-is.
    """
    _raise_rewritten_in_app_cdp_error(exc)
    message = _redact_cdp_token(str(exc))
    if message != str(exc):
        raise RuntimeError(message) from None
    raise


def _public_tool(fn: Callable[_P, str]) -> Callable[_P, str]:
    """Register-time wrapper that keeps CDP tokens out of tool failures.

    ``BU_CDP_WS`` carries the in-app bridge token, browser-harness logs the
    endpoint it connects to, and FastMCP turns any raised exception into
    agent-visible ``ToolError`` text. Redacting inside individual helpers only
    covers the call sites someone remembered, so every tool goes through here.
    """

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
        with _TOOL_LOCK:
            with perf.timed_tool(fn.__name__) as rec:
                try:
                    result = fn(*args, **kwargs)
                    rec.observe(result)
                    return result
                except Exception as exc:
                    rec.fail()
                    _reraise_public_harness_error(exc)

    return wrapper


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
    helpers, admin = _bh
    is_agentcore = _bound_cdp == "agentcore"
    with contextlib.suppress(Exception):
        tabs.registry().detach_all(helpers)
    _bh = None
    dom_indexing.clear_registered_targets()
    if is_agentcore:
        admin.stop_session()
        from browser_mcp import playwright_helpers

        playwright_helpers.disconnect()
    else:
        from browser_harness import admin as bh_admin

        bh_admin.restart_daemon()


def _with_perf_helpers(bh: tuple[Any, Any]) -> tuple[Any, Any]:
    helpers, admin = bh
    return perf.wrap_helpers(helpers), admin


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
            return _with_perf_helpers(_bh)
        _teardown_bound_backend()
        return _with_perf_helpers(_agentcore_browser_harness())

    if _bh is not None and cdp == _bound_cdp:
        return _with_perf_helpers(_bh)

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
    admin.ensure_daemon()
    _bh = (helpers, admin)
    _bound_cdp = cdp
    return _with_perf_helpers(_bh)


def _json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


_LOAD_WAIT_JS = (
    "document.readyState==='complete' ? true : new Promise((resolve) => {"
    "const t = setTimeout(() => resolve(false), 15000);"
    "addEventListener('load', () => { clearTimeout(t); resolve(true); }, {once:true});"
    "})"
)

_FILL_MODES = frozenset({"auto", "keys", "fast"})
_ELEMENT_KINDS = frozenset({"inputs", "buttons", "links", "all"})
_OBSERVE_MODES = frozenset({"full", "diff"})
_VIEWPORT_OFF = frozenset({"0", "false", "no", "off"})
_VIEWPORT_ON = frozenset({"1", "true", "yes", "on"})


def _is_blank_url(url: str) -> bool:
    url = url or ""
    return url in ("", "about:blank") or url.startswith("about:blank#")


def _can_goto_in_place(helpers: Any) -> bool:
    if not callable(getattr(helpers, "goto_url", None)):
        return False
    url = ""
    if callable(getattr(helpers, "current_tab", None)):
        try:
            tab = helpers.current_tab()
        except Exception:
            tab = None
        if isinstance(tab, dict):
            url = str(tab.get("url") or "")
    if not url and callable(getattr(helpers, "page_info", None)):
        try:
            info = helpers.page_info() or {}
            url = str(info.get("url") or "")
        except Exception:
            url = ""
    return not _is_blank_url(url)


def _resolve_fill_mode(mode: str) -> str:
    raw = (mode or "auto").strip().lower()
    if raw == "auto":
        raw = (os.environ.get("BROWSER_MCP_FILL_MODE") or "auto").strip().lower()
    return raw if raw in _FILL_MODES else "auto"


def _resolve_viewport_only(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = (os.environ.get("BROWSER_MCP_VIEWPORT_DEFAULT") or "1").strip().lower()
    if raw in _VIEWPORT_OFF:
        return False
    if raw in _VIEWPORT_ON:
        return True
    return True


def _cache_tree(handle: tabs.TabHandle, url: str | None, lines: list[str]) -> None:
    state = handle.state
    if state is None:
        return
    state.last_tree = list(lines)
    if url:
        state.last_url = url


def _unknown_tab_result(exc: tabs.UnknownTabError) -> str:
    return _json_text({"ok": False, "error": str(exc)})


def _for_read(helpers: Any, tab: str | None) -> tabs.TabHandle:
    """Resolve a tab for a read. Never calls switch_tab."""
    reg = tabs.registry()
    if tab is None:
        focused = reg.focused()
        if focused is not None:
            reg.mark_used(focused)
            return reg.focused_handle(helpers, focused)
        return tabs.TabHandle(helpers, None, focused=True)
    reg.refresh(helpers)
    state = reg.resolve(tab)
    focused = state.target_id == reg.focused_id
    handle = reg.handle(helpers, state, focused=focused)
    reg.mark_used(state)
    return handle


def _for_action(helpers: Any, tab: str | None) -> tabs.TabHandle:
    """Resolve a tab for an action. Focuses it first when it is not already focused."""
    reg = tabs.registry()
    if tab is None:
        focused = reg.focused()
        if focused is not None:
            reg.mark_used(focused)
            return reg.focused_handle(helpers, focused)
        return tabs.TabHandle(helpers, None, focused=True)
    reg.refresh(helpers)
    state = reg.resolve(tab)
    sid = None
    if state.target_id != reg.focused_id:
        sid = helpers.switch_tab(state.target_id)
        reg.set_focused(state.target_id)
    handle = reg.focused_handle(helpers, state)
    handle.switch_session_id = sid if isinstance(sid, str) else None
    reg.mark_used(state)
    return handle


def _close_target(helpers: Any, target_id: str) -> None:
    if callable(getattr(helpers, "close_tab", None)):
        helpers.close_tab(target_id)
        return
    if callable(getattr(helpers, "cdp", None)):
        helpers.cdp("Target.closeTarget", targetId=target_id)
        return
    raise tabs.SingleTabBackendError()


def _create_blank_target(helpers: Any, *, focus: bool, url: str = "about:blank") -> bool:
    """Create a tab. Returns True if ``url`` was already loaded by the helper."""
    if callable(getattr(helpers, "cdp", None)):
        try:
            result = helpers.cdp(
                "Target.createTarget", url="about:blank", background=not focus
            )
        except TypeError:
            result = helpers.cdp("Target.createTarget", url="about:blank")
        except Exception as exc:
            if tabs.is_single_tab_error(exc):
                raise tabs.SingleTabBackendError() from exc
            try:
                result = helpers.cdp("Target.createTarget", url="about:blank")
            except Exception as inner:
                if tabs.is_single_tab_error(inner):
                    raise tabs.SingleTabBackendError() from inner
                raise
        if not isinstance(result, dict) or not result.get("targetId"):
            raise tabs.SingleTabBackendError()
        tid = str(result["targetId"])
        if focus:
            helpers.switch_tab(tid)
        return False
    if callable(getattr(helpers, "new_tab", None)):
        try:
            helpers.new_tab(url, background=not focus)
        except TypeError:
            helpers.new_tab(url)
        except Exception as exc:
            if tabs.is_single_tab_error(exc):
                raise tabs.SingleTabBackendError() from exc
            raise
        return True
    raise tabs.SingleTabBackendError()


def _open_tab(
    helpers: Any,
    url: str,
    *,
    alias: str | None,
    focus: bool,
    opened_by_agent: bool = True,
) -> dict[str, Any]:
    reg = tabs.registry()
    reg.refresh(helpers)
    if reg.would_exceed_cap():
        return reg.cap_error_payload()
    try:
        already_at_url = _create_blank_target(helpers, focus=focus, url=url)
    except tabs.SingleTabBackendError as exc:
        return {"ok": False, "error": str(exc)}
    state = reg.remember_created(helpers, opened_by_agent=opened_by_agent, alias=alias)
    handle = _for_action(helpers, state.tab) if focus else _for_read(helpers, state.tab)
    if url and url != "about:blank" and not already_at_url:
        handle.navigate(url)
    if url and url != "about:blank":
        handle.evaluate(_LOAD_WAIT_JS)
    dom_indexing._register_driver_for_new_documents(handle)
    dom_indexing.settle(handle)
    info = handle.page_info()
    state.url = str(info.get("url") or state.url)
    state.title = str(info.get("title") or state.title)
    state.last_url = state.url
    return {
        "ok": True,
        "tab": state.tab,
        "alias": state.alias,
        "url": state.url,
        "title": state.title,
        "focused": state.target_id == tabs.registry().focused_id,
    }


def _close_agent_opened_tabs(helpers: Any) -> None:
    reg = tabs.registry()
    with contextlib.suppress(Exception):
        reg.refresh(helpers)
    for state in list(reg.agent_opened()):
        with contextlib.suppress(Exception):
            _close_target(helpers, state.target_id)
    with contextlib.suppress(Exception):
        reg.refresh(helpers)


@mcp.tool()
@_public_tool
def browser_goto(url: str, new_tab: bool = False, tab: str | None = None) -> str:
    """Navigate to a URL in the current tab (or a new tab if new_tab=True / current is blank).

    Pass tab= to navigate a specific tab in place without focusing it. new_tab=True
    opens a new tab and focuses it (same as browser_open_tab with focus=True).

    Returns page info and matching playbook filenames.
    """
    helpers, _ = _browser_harness()
    if new_tab:
        opened = _open_tab(helpers, url, alias=None, focus=True)
        if not opened.get("ok"):
            return _json_text(opened)
        info = {
            "url": opened.get("url"),
            "title": opened.get("title"),
            "tab": opened.get("tab"),
            "alias": opened.get("alias"),
        }
        names = playbooks.list_playbook_names(url)
        return _json_text({**info, "playbooks": names})
    if tab is not None:
        try:
            handle = _for_read(helpers, tab)
        except tabs.UnknownTabError as exc:
            return _unknown_tab_result(exc)
        handle.navigate(url)
        handle.evaluate(_LOAD_WAIT_JS)
        dom_indexing._register_driver_for_new_documents(handle)
        dom_indexing.settle(handle)
        info = handle.page_info()
        names = playbooks.list_playbook_names(url)
        return _json_text({**info, "playbooks": names})
    if not _can_goto_in_place(helpers):
        helpers.new_tab(url)
        dom_indexing.mark_driver_stale()
    else:
        helpers.goto_url(url)
    helpers.js(_LOAD_WAIT_JS)
    dom_indexing._register_driver_for_new_documents(helpers)
    dom_indexing.settle(helpers)
    info = helpers.page_info()
    names = playbooks.list_playbook_names(url)
    return _json_text({**info, "playbooks": names})


@mcp.tool()
@_public_tool
def browser_get_elements(
    viewport_only: bool | None = None,
    kind: str | None = None,
    contains: str | None = None,
    max_elements: int = 150,
    observe: str = "full",
    tab: str | None = None,
) -> str:
    """Return interactive elements as an indexed text tree — the default way to see the page.

    Prefer this over browser_screenshot for locating things to click/type into. Injects a
    DOM-walker (vendored from alibaba/page-agent, MIT licensed) that scans the live DOM and
    returns lines like ``[12]<input placeholder='Email' />`` / ``[35]<button>Submit</button>``,
    indentation shows parent/child nesting. Use the numeric index with browser_click_by_index /
    browser_input_by_index / browser_select_by_index — no coordinates needed.

    Defaults to the visible viewport (override with viewport_only=false or
    BROWSER_MCP_VIEWPORT_DEFAULT=0). kind is inputs/buttons/links/all; contains is a
    case-insensitive substring on the rendered line (searches the whole page, including
    below the fold); max_elements (default 150) truncates with a footer. Indices remain
    valid until navigation — re-call after navigation, or when the footer says to scroll.
    observe="diff" returns added/removed lines vs the last tree for this tab (falls back
    to a full tree after navigation or with no cache). Pass tab= to read a background tab
    without moving focus.
    """
    kind_norm: str | None = None
    if kind is not None and str(kind).strip():
        kind_norm = str(kind).strip().lower()
        if kind_norm not in _ELEMENT_KINDS:
            return _json_text(
                {
                    "ok": False,
                    "error": (
                        f"unknown kind {kind!r}; expected inputs, buttons, links, or all"
                    ),
                }
            )
        if kind_norm == "all":
            kind_norm = None
    observe_norm = (observe or "full").strip().lower()
    if observe_norm not in _OBSERVE_MODES:
        return _json_text(
            {"ok": False, "error": f"unknown observe {observe!r}; expected full or diff"}
        )
    try:
        cap = max(1, int(max_elements))
    except (TypeError, ValueError):
        return _json_text({"ok": False, "error": "max_elements must be an integer"})
    viewport = _resolve_viewport_only(viewport_only)
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    result = dom_indexing.get_elements(
        handle,
        viewport,
        kind=kind_norm,
        contains=contains,
        max_elements=cap,
    )
    if result.get("error"):
        return _json_text({"ok": False, **result})
    raw_tree = str(result.get("tree") or "")
    lines = dom_indexing.tree_lines(raw_tree)
    truncated = bool(result.get("truncated"))
    below_viewport = int(result.get("below_viewport") or 0)
    omitted = int(result.get("omitted") or 0)
    url = str(result.get("url") or "")
    title = str(result.get("title") or "")
    element_count = int(result.get("elementCount") or 0)
    searching = bool(contains and str(contains).strip())
    tree = dom_indexing.attach_tree_footers(
        raw_tree,
        viewport_only=viewport and not searching,
        below_viewport=below_viewport,
        truncated=truncated,
        omitted=omitted,
    )
    navigated = bool(handle.state and handle.state.last_url and url and handle.state.last_url != url)
    use_diff = observe_norm == "diff"
    previous = handle.state.last_tree if handle.state is not None else None
    if use_diff and previous is not None and not navigated:
        diff = dom_indexing.diff_tree_lines(previous, lines)
        _cache_tree(handle, url, lines)
        return _json_text(
            {
                "ok": True,
                "mode": "diff",
                "added": diff["added"],
                "removed": diff["removed"],
                "unchanged": diff["unchanged"],
                "elementCount": element_count,
                "url": url,
                "title": title,
                "truncated": truncated,
                "below_viewport": below_viewport,
            }
        )
    _cache_tree(handle, url, lines)
    payload: dict[str, Any] = {
        "ok": True,
        "url": url,
        "title": title,
        "elementCount": element_count,
        "tree": tree,
        "truncated": truncated,
        "below_viewport": below_viewport,
    }
    if use_diff:
        payload["mode"] = "full"
    return _json_text(payload)


@mcp.tool()
@_public_tool
def browser_get_text(
    max_chars: int = 8000,
    selector: str | None = None,
    tab: str | None = None,
) -> str:
    """Readable page text for reading, separate from the interactive element tree.

    Prefers ``<main>`` / ``<article>`` / ``[role=main]``, else ``body``. Strips
    script, style, nav, footer, and aside. Pass selector to read a specific
    subtree. Use this instead of ``browser_js("document.body.innerText")``.
    """
    try:
        cap = max(1, int(max_chars))
    except (TypeError, ValueError):
        return _json_text({"ok": False, "error": "max_chars must be an integer"})
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    text = handle.readable_text(selector=selector)
    truncated = len(text) > cap
    if truncated:
        text = text[:cap]
    info = handle.page_info()
    return _json_text(
        {
            "ok": True,
            "text": text,
            "truncated": truncated,
            "url": info.get("url") or "",
            "title": info.get("title") or "",
        }
    )


@mcp.tool()
@_public_tool
def browser_click_by_index(index: int, tab: str | None = None) -> str:
    """Click an interactive element by index from browser_get_elements. Prefer this over
    browser_click(x, y) — no pixel-guessing, resilient to layout shifts."""
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    try:
        rect = dom_indexing.get_rect(handle, index)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    helpers.click_at_xy(rect["x"], rect["y"])
    payload: dict[str, Any] = {"ok": True, "clicked": rect}
    obscured = rect.get("obscuredBy")
    if obscured:
        payload["warning"] = f"target obscured by {obscured}"
    return _json_text(payload)


@mcp.tool()
@_public_tool
def browser_input_by_index(
    index: int, text: str, clear_first: bool = True, mode: str = "auto", tab: str | None = None
) -> str:
    """Click and type text into an indexed input/textarea/contenteditable element from
    browser_get_elements. Prefer this over screenshot + coordinate typing.

    mode: ``auto`` (default) tries an in-page value set and falls back to real key
    events; ``keys`` always types; ``fast`` never falls back. Pass ``keys`` for
    comboboxes and fields that only listen to keydown. Override the default with
    BROWSER_MCP_FILL_MODE.
    """
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    try:
        result = dom_indexing.fill(
            handle, index, text, clear_first=clear_first, mode=_resolve_fill_mode(mode)
        )
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text(
        {
            "ok": True,
            "index": index,
            "tagName": result.get("tagName"),
            "mode_used": result.get("mode_used"),
        }
    )


@mcp.tool()
@_public_tool
def browser_select_by_index(index: int, text: str, tab: str | None = None) -> str:
    """Select a <select> dropdown option by visible text, using the index from
    browser_get_elements."""
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    try:
        dom_indexing.select_option(handle, index, text)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text({"ok": True, "index": index, "selected": text})


@mcp.tool()
@_public_tool
def browser_screenshot(
    full: bool = False,
    max_dim: int | None = 1200,
    format: str = "jpeg",
    quality: int = 60,
    annotate: bool = False,
    tab: str | None = None,
) -> str:
    """Capture the current viewport as a JPEG (PNG available).

    LAST-RESORT FALLBACK: use browser_get_elements + browser_click_by_index /
    browser_input_by_index for ordinary clicking and typing instead — it's cheaper (no
    image tokens) and more reliable (indexed elements vs. guessed pixels). Reach for
    this tool only when browser_get_elements doesn't surface what you need: canvas-based
    apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming rendering/layout.
    Pass ``annotate=True`` to draw current index labels so the next click can still
    use browser_click_by_index.

    Returns JSON with a workspace-relative path under ``./browser/Screenshots/`` (for
    ``load_file`` on vision models), ``bytes`` (file size), and ``format`` — not inline
    image bytes.
    """
    fmt = (format or "jpeg").strip().lower()
    if fmt in {"jpg", "jpeg"}:
        fmt = "jpeg"
    elif fmt != "png":
        return _json_text({"ok": False, "error": "format must be jpeg or png"})
    try:
        q = int(quality)
    except (TypeError, ValueError):
        return _json_text({"ok": False, "error": "quality must be 1–95"})
    if q < 1 or q > 95:
        return _json_text({"ok": False, "error": "quality must be 1–95"})

    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)

    dest, rel_path = screenshots.allocate_screenshot_path(fmt)
    native = dest.with_name(f"{dest.stem}-native.png")
    annotated_img = None
    labeled = 0
    try:
        handle.capture_screenshot(path=str(native), full=full, max_dim=None)
        if annotate:
            map_len = 0
            try:
                raw = handle.evaluate("Object.keys(window.__bmcpSelectorMap || {}).length")
                map_len = int(raw or 0)
            except (TypeError, ValueError):
                map_len = 0
            except Exception:
                map_len = 0
            if not map_len:
                dom_indexing.get_elements(handle, viewport_only=not full)
            payload = dom_indexing.get_rects(handle, scroll=False, full=full)
            from PIL import Image

            with Image.open(native) as captured:
                annotated_img, labeled = screenshots.draw_index_labels(
                    captured,
                    payload.get("rects") or {},
                    css_width=float(payload.get("cssWidth") or 0),
                    css_height=float(payload.get("cssHeight") or 0),
                )
        screenshots.encode_screenshot(
            native,
            dest,
            fmt=fmt,
            quality=q,
            max_dim=max_dim,
            image=annotated_img,
        )
    finally:
        native.unlink(missing_ok=True)
        if annotated_img is not None:
            annotated_img.close()

    info = handle.page_info()
    note = (
        "Screenshot saved under the agent workspace. Vision models: call load_file "
        "with path. Text-only models: use browser_get_elements instead of this tool. "
        "Coordinate clicks use viewport metadata from this capture."
    )
    if annotate:
        note = (
            "Labels correspond to the current get_elements indices. Vision models: "
            "call load_file with path, then browser_click_by_index. Text-only models: "
            "use browser_get_elements instead of this tool."
        )
    payload = {
        "ok": True,
        "path": rel_path,
        "screenshots_dir": "./browser/Screenshots",
        "url": info.get("url"),
        "title": info.get("title"),
        "viewport": {"w": info.get("w"), "h": info.get("h")},
        "bytes": dest.stat().st_size,
        "format": fmt,
        "note": note,
    }
    if annotate:
        payload["annotated"] = True
        payload["labeled"] = labeled
    return _json_text(payload)


@mcp.tool()
@_public_tool
def browser_click(
    x: float, y: float, button: str = "left", clicks: int = 1, tab: str | None = None
) -> str:
    """Click at viewport coordinates (x, y).

    LAST-RESORT FALLBACK: prefer browser_get_elements + browser_click_by_index for
    ordinary clicking. Only use raw coordinates for elements browser_get_elements can't
    represent (canvas, shadow DOM, drag targets) or after visually confirming a spot via
    browser_screenshot.
    """
    helpers, _ = _browser_harness()
    try:
        _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    helpers.click_at_xy(x, y, button=button, clicks=clicks)
    return _json_text({"ok": True})


@mcp.tool()
@_public_tool
def browser_fill(
    selector: str,
    text: str,
    clear_first: bool = True,
    timeout: float = 0.0,
    tab: str | None = None,
) -> str:
    """Fill a form input (works with React/Vue controlled inputs)."""
    helpers, _ = _browser_harness()
    try:
        _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    helpers.fill_input(selector, text, clear_first=clear_first, timeout=timeout)
    return _json_text({"ok": True})


@mcp.tool()
@_public_tool
def browser_press_key(key: str, modifiers: int = 0, tab: str | None = None) -> str:
    """Press a key. Modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift."""
    helpers, _ = _browser_harness()
    try:
        _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    helpers.press_key(key, modifiers=modifiers)
    return _json_text({"ok": True})


@mcp.tool()
@_public_tool
def browser_scroll(
    x: float, y: float, dy: float = -300, dx: float = 0, tab: str | None = None
) -> str:
    """Scroll the page at viewport position (x, y)."""
    helpers, _ = _browser_harness()
    try:
        _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    helpers.scroll(x, y, dy=dy, dx=dx)
    return _json_text({"ok": True})


@mcp.tool()
@_public_tool
def browser_js(expression: str, tab: str | None = None) -> str:
    """Evaluate JavaScript in the attached tab and return the result (DOM read/extraction)."""
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    result = handle.evaluate(expression)
    return _json_text({"ok": True, "result": result})


@mcp.tool()
@_public_tool
def browser_wait_for(
    selector: str, visible: bool = False, timeout: float = 10.0, tab: str | None = None
) -> str:
    """Wait until an element matching selector exists (optionally visible)."""
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    result = dom_indexing.wait_for_selector(
        handle, selector, visible=visible, timeout=timeout
    )
    found = bool(result.get("found"))
    return _json_text({"ok": found, "found": found})


@mcp.tool()
@_public_tool
def browser_wait_idle(
    timeout: float = 10.0, idle_ms: float = 500, tab: str | None = None
) -> str:
    """Wait until network activity is idle, then until the DOM is quiet.

    Network idle is only available on the focused tab. On another tab this falls
    back to a DOM settle and reports idle as null.
    """
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    if not handle.focused:
        settled = dom_indexing.settle(handle)
        return _json_text(
            {
                "ok": True,
                "idle": None,
                "quiet": bool(settled.get("quiet", True)),
                "navigated": bool(settled.get("navigated", False)),
                "note": _WAIT_IDLE_NOTE,
            }
        )
    wait_idle = getattr(helpers, "wait_for_network_idle", None)
    if not callable(wait_idle):
        settled = dom_indexing.settle(handle)
        return _json_text(
            {
                "ok": True,
                "idle": None,
                "quiet": bool(settled.get("quiet", True)),
                "navigated": bool(settled.get("navigated", False)),
                "note": "network idle is not available on this backend; DOM settle was used",
            }
        )
    idle = bool(wait_idle(timeout=timeout, idle_ms=idle_ms))
    if not idle:
        return _json_text(
            {"ok": False, "idle": False, "quiet": False, "navigated": False}
        )
    settled = dom_indexing.settle(handle)
    return _json_text(
        {
            "ok": True,
            "idle": True,
            "quiet": bool(settled.get("quiet", True)),
            "navigated": bool(settled.get("navigated", False)),
        }
    )


@mcp.tool()
@_public_tool
def browser_page_info(tab: str | None = None) -> str:
    """Return current page url, title, viewport size, and scroll position."""
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    return _json_text(handle.page_info())


@mcp.tool()
@_public_tool
def browser_tabs() -> str:
    """List open browser tabs with aliases, focus, and last-used times."""
    helpers, _ = _browser_harness()
    reg = tabs.registry()
    reg.refresh(helpers)
    return _json_text(reg.list_payload())


@mcp.tool()
@_public_tool
def browser_switch_tab(target_id: str) -> str:
    """Switch to a tab by alias or target_id (from browser_tabs)."""
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, target_id)
    except tabs.UnknownTabError:
        sid = helpers.switch_tab(target_id)
        dom_indexing.mark_driver_stale()
        dom_indexing._register_driver_for_new_documents(helpers)
        return _json_text({"ok": True, "session_id": sid})
    sid = handle.switch_session_id
    if sid is None and handle.state is not None:
        sid = helpers.switch_tab(handle.state.target_id)
    dom_indexing._register_driver_for_new_documents(handle)
    return _json_text({"ok": True, "session_id": sid})


@mcp.tool()
@_public_tool
def browser_open_tab(url: str, alias: str | None = None, focus: bool = False) -> str:
    """Open a URL in a new tab. Defaults to the background (does not steal focus).

    alias must match [a-z][a-z0-9_-]{0,23}. At most five agent-controlled tabs;
    on the cap this returns tab_limit_reached and does not open or close anything.
    """
    helpers, _ = _browser_harness()
    try:
        return _json_text(_open_tab(helpers, url, alias=alias, focus=focus))
    except (ValueError, tabs.UnknownTabError) as exc:
        return _json_text({"ok": False, "error": str(exc)})


@mcp.tool()
@_public_tool
def browser_close_tab(tab: str) -> str:
    """Close a tab by alias or target id.

    Refuses to close the last tab (navigates it to about:blank instead). If the
    closed tab was focused, focuses the most recently used remaining tab.
    """
    helpers, _ = _browser_harness()
    reg = tabs.registry()
    try:
        reg.refresh(helpers)
        state = reg.resolve(tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    was_focused = state.target_id == reg.focused_id
    remaining = [s for s in reg.tabs() if s.target_id != state.target_id]
    if not remaining:
        handle = _for_action(helpers, state.tab)
        handle.navigate("about:blank")
        state.url = "about:blank"
        state.title = ""
        return _json_text(
            {
                "ok": True,
                "closed": False,
                "blanked": state.tab,
                "focused": state.tab,
                "note": "last tab was navigated to about:blank instead of closed",
            }
        )
    _close_target(helpers, state.target_id)
    closed = state.tab
    reg.refresh(helpers)
    focused_tab = None
    if was_focused:
        nxt = reg.most_recently_used()
        if nxt is not None:
            helpers.switch_tab(nxt.target_id)
            reg.set_focused(nxt.target_id)
            focused_tab = nxt.tab
            dom_indexing._register_driver_for_new_documents(reg.focused_handle(helpers, nxt))
    else:
        focused = reg.focused()
        focused_tab = focused.tab if focused else None
    return _json_text({"ok": True, "closed": closed, "focused": focused_tab})


@mcp.tool()
@_public_tool
def browser_read_tabs(
    tabs: list[str] | None = None,
    mode: str = "text",
    max_chars: int = 3000,
) -> str:
    """Fan-out read over tabs without changing focus.

    ``tabs`` defaults to agent-opened tabs. mode is ``text`` (readable body) or
    ``tree`` (indexed element tree). Sequential; one IPC call per tab.
    """
    selected = tabs
    helpers, _ = _browser_harness()
    from browser_mcp.tabs import UnknownTabError, registry as get_registry

    reg = get_registry()
    reg.refresh(helpers)
    if selected:
        try:
            states = [reg.resolve(name) for name in selected]
        except UnknownTabError as exc:
            return _unknown_tab_result(exc)
    else:
        states = reg.agent_opened()
    out: list[dict[str, Any]] = []
    mode_norm = (mode or "text").strip().lower()
    cap = max(1, int(max_chars))
    for state in states:
        handle = _for_read(helpers, state.tab)
        info = handle.page_info()
        entry: dict[str, Any] = {
            "tab": state.tab,
            "alias": state.alias,
            "url": info.get("url") or state.url,
            "title": info.get("title") or state.title,
            "truncated": False,
        }
        if mode_norm == "tree":
            tree = dom_indexing.get_elements(handle, viewport_only=True)
            text = str(tree.get("tree") or "")
            if len(text) > cap:
                entry["tree"] = text[:cap]
                entry["truncated"] = True
            else:
                entry["tree"] = text
        else:
            text = handle.readable_text()
            if len(text) > cap:
                entry["text"] = text[:cap]
                entry["truncated"] = True
            else:
                entry["text"] = text
        out.append(entry)
    return _json_text({"ok": True, "tabs": out})


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
    token = _query_token(urlparse(file_url).query) or _read_in_app_cdp_token()
    return http, token


def _public_login_result(body: dict[str, Any]) -> dict[str, Any]:
    """Copy only the non-secret fields out of the bridge response.

    Built by allowlist rather than by stripping keys, so a future bridge field
    cannot introduce a credential leak here.
    """
    error = body.get("error")
    origin = body.get("origin")
    result: dict[str, Any] = {
        "ok": bool(body.get("ok")),
        "loggedIn": bool(body.get("loggedIn")),
    }
    # The origin the bridge actually acted on. The agent cannot otherwise tell:
    # the bridge logs in to the tab the *user* has focused, while harness tool
    # calls address tabs by CDP session, so browser_switch_tab (or the user
    # clicking another tab) makes the two diverge.
    if isinstance(origin, str) and origin:
        result["origin"] = origin
    if error:
        message = str(error)
        result["error"] = message if message in _LOGIN_PUBLIC_ERRORS else "login failed"
    return result


def _loopback_open(req: urllib.request.Request, timeout: float = _LOGIN_TIMEOUT_S) -> Any:
    return _LOOPBACK_OPENER.open(req, timeout=timeout)


def _sealed_login(username: str | None, expected_origin: str | None) -> dict[str, Any]:
    if _bound_cdp == "agentcore":
        return {"ok": False, "loggedIn": False, "error": "in-app browser is not available"}
    http, token = _in_app_http_and_token()
    if not http:
        return {"ok": False, "loggedIn": False, "error": "in-app browser is not available"}
    if not token:
        return {"ok": False, "loggedIn": False, "error": "in-app browser token is missing"}
    request_body: dict[str, str] = {}
    if username:
        request_body["username"] = username
    if expected_origin:
        request_body["expectedOrigin"] = expected_origin
    payload = json.dumps(request_body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(f"{http}/json/login", data=payload, headers=headers, method="POST")
    try:
        with _loopback_open(req, timeout=_LOGIN_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) in {401, 403}:
            return {"ok": False, "loggedIn": False, "error": "in-app browser token is missing"}
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
    result = _public_login_result(body)
    if expected_origin and "origin" not in result:
        # A Spaces build older than expectedOrigin support ignores it and echoes
        # no origin, so the login it just performed is unverifiable. Report that
        # rather than letting the agent treat an unchecked login as confirmed.
        result = {
            "ok": False,
            "loggedIn": result["loggedIn"],
            "error": "in-app browser could not verify the origin",
        }
    return result


@mcp.tool()
@_public_tool
def browser_login(username: str | None = None, expected_origin: str | None = None) -> str:
    """Log in with a saved Spaces password without revealing the credential.

    Call this only when the user asked to sign in. Do not type or read the
    password. Returns {ok, loggedIn, origin} — never the password value.

    This signs in to the tab the user has focused, which is not necessarily the
    tab your other browser_* calls address. Pass expected_origin (e.g.
    "https://example.com") to make the bridge refuse rather than sign in to a
    different site; always check the returned origin before reporting success.
    """
    result = _sealed_login(username, expected_origin)
    return _json_text(result)


@mcp.tool()
@_public_tool
def browser_upload(selector: str, path: str, tab: str | None = None) -> str:
    """Set files on a file input. path must be an absolute filepath on the host."""
    helpers, _ = _browser_harness()
    try:
        _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    helpers.upload_file(selector, path)
    return _json_text({"ok": True})


@mcp.tool()
@_public_tool
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
@_public_tool
def browser_read_playbook(host: str) -> str:
    """Read the playbook markdown for a host or URL."""
    try:
        content = playbooks.read_playbook(host)
    except playbooks.PlaybookError as exc:
        logger.warning("browser_read_playbook failed for host=%r: %s", host, exc)
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text({"ok": True, "host": playbooks.host_slug(host), "content": content})


@mcp.tool()
@_public_tool
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
        if _bh is not None:
            helpers, _ = _bh
            with contextlib.suppress(Exception):
                _close_agent_opened_tabs(helpers)
        _teardown_bound_backend()
        return
    if _bh is not None:
        helpers, _ = _bh
        with contextlib.suppress(Exception):
            _close_agent_opened_tabs(helpers)
    _bh = None
    from browser_harness import admin

    admin.restart_daemon()


@mcp.tool()
@_public_tool
def browser_stop() -> str:
    """Stop the active browser backend (cleanup after browsing; important for cloud/AgentCore browsers)."""
    global _bound_cdp
    try:
        _stop_active_backend_best_effort()
    except Exception as exc:
        _bound_cdp = None
        # Log the message rather than exc_info: a harness traceback carries the
        # tokenized endpoint, and this log is not necessarily private.
        message = _redact_cdp_token(str(exc))
        logger.warning("browser_stop: failed to stop browser backend: %s", message)
        return _json_text({"ok": False, "error": message})
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
    except Exception as exc:
        logger.warning(
            "browser-mcp shutdown: failed to stop browser backend: %s",
            _redact_cdp_token(str(exc)),
        )
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
