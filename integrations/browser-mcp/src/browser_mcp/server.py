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
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, ParamSpec
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from mcp.server.fastmcp import FastMCP

from browser_mcp import actions, agentcore, dom_indexing, perf, playbooks, screenshots, tabs

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
        "1. Prefer intent tools when you already know the labels or the multi-step "
        "flow: browser_fill_form for forms, browser_click_text when the visible label "
        "is known, browser_act for a batch of clicks/inputs/waits (cap 25 steps), "
        "browser_extract for structured scraping instead of browser_js.\n"
        "2. Otherwise browser_get_elements() once to see the indexed tree (viewport "
        "by default). Use kind=/contains= to narrow, viewport_only=false or scroll "
        "when the footer says more are below, and browser_get_text for readable page "
        "text.\n"
        "3. browser_click_by_index / browser_input_by_index / browser_select_by_index "
        "to act. Each action settles and returns observation (diff by default) — read "
        "that instead of calling get_elements again. Indices remain valid until "
        "navigation. Pass observe=\"none\" to skip the snapshot, observe=\"full\" for "
        "the whole viewport tree. Only call browser_get_elements again when you need "
        "a different filter, the whole tree, or after navigation if the observation "
        "is not enough. browser_goto returns a full observation by default.\n"
        "\n"
        "browser_screenshot is a LAST-RESORT FALLBACK only — use it when "
        "browser_get_elements returns nothing useful (canvas apps, heavily shadow-DOM "
        "UIs, drag-and-drop, or visually confirming layout/rendering). Prefer "
        "browser_screenshot(annotate=True) then browser_click_by_index when indices "
        "are available; browser_click(x, y) is last-resort after that. Do not default "
        "to screenshots for ordinary clicking/typing.\n"
        "\n"
        "Check browser_list_playbooks before improvising on a site. If flows are listed, "
        "call browser_run_playbook(host, name, params) instead of re-planning. "
        "Read markdown playbooks only for notes. On a failed_step, continue by hand and "
        "browser_write_playbook(..., append=true) with a corrected ```playbook fence. "
        "browser_recent_actions(host) lists what actually worked (labels and lengths, not typed text). "
        "If the user asked to sign in on the Spaces in-app browser and a saved "
        "password exists, call browser_login(expected_origin=...) — never read or "
        "type the password yourself, and check the returned origin. "
        "Call browser_stop when done with remote/cloud sessions.\n"
        "\n"
        "Tabs: each tab has a short alias (t1, t2, …) or a name you pass to "
        "browser_open_tab(alias=...). Reads (get_elements, get_text, page_info, js, wait_for, "
        "read_tabs) never move focus — pass tab= to address a background tab. "
        "Actions (click, input, select, fill, fill_form, click_text, act, screenshot, …) focus the tab first "
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


_LOAD_WAIT_JS = actions.LOAD_WAIT_JS
_ELEMENT_KINDS = frozenset({"inputs", "buttons", "links", "all"})
_OBSERVE_MODES = frozenset({"full", "diff"})
_ACTION_OBSERVE_MODES = frozenset({"full", "diff", "none"})
_VIEWPORT_OFF = frozenset({"0", "false", "no", "off"})
_VIEWPORT_ON = frozenset({"1", "true", "yes", "on"})
_DIFF_TO_FULL_RATIO = 0.6
_NETWORK_STARTED = "Network.requestWillBeSent"
_NETWORK_ENDED = frozenset(
    {
        "Network.loadingFinished",
        "Network.loadingFailed",
        "Network.loadingCancelled",
    }
)


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
        logger.debug(
            "cache_tree skipped: handle has no TabState (tree_n=%s url=%s)",
            len(lines),
            url,
        )
        return
    state.last_tree = list(lines)
    if url:
        state.last_url = url
    logger.debug(
        "cache_tree target=%s tree_n=%s url=%s",
        state.target_id,
        len(state.last_tree),
        state.last_url,
    )


def _env_ms(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _quiet_ms() -> int:
    return _env_ms("BROWSER_MCP_QUIET_MS", 150)


def _settle_ms() -> int:
    return _env_ms("BROWSER_MCP_SETTLE_MS", 1500)


def _resolve_action_observe(value: str | None, *, default: str = "diff") -> str:
    if value is not None and str(value).strip():
        return str(value).strip().lower()
    if default != "diff":
        return default
    env = (os.environ.get("BROWSER_MCP_OBSERVE_DEFAULT") or "diff").strip().lower()
    return env if env in _ACTION_OBSERVE_MODES else "diff"


def _observe_error(value: object, allowed: frozenset[str]) -> str:
    names = ", ".join(sorted(allowed))
    return _json_text(
        {"ok": False, "error": f"unknown observe {value!r}; expected {names}"}
    )


def _remaining_ms(started: float, budget_ms: int) -> int:
    used = int((time.monotonic() - started) * 1000)
    return max(0, budget_ms - used)


def _network_in_flight(helpers: Any, in_flight: set[str]) -> bool:
    drain = getattr(helpers, "drain_events", None)
    if not callable(drain):
        return bool(in_flight)
    try:
        events = drain()
    except Exception:
        return bool(in_flight)
    if not isinstance(events, (list, tuple)):
        return bool(in_flight)
    for event in events:
        if not isinstance(event, dict):
            continue
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        req_id = params.get("requestId")
        if not req_id:
            continue
        key = str(req_id)
        if method == _NETWORK_STARTED:
            in_flight.add(key)
        elif method in _NETWORK_ENDED:
            in_flight.discard(key)
    return bool(in_flight)


def _wait_while_network_busy(
    handle: tabs.TabHandle, *, remaining_ms: int, quiet_ms: int
) -> dict[str, Any]:
    helpers = handle.helpers
    in_flight: set[str] = set()
    if _network_in_flight(helpers, in_flight):
        extra = {"quiet": True, "navigated": False}
        deadline = time.monotonic() + remaining_ms / 1000.0
        while in_flight and time.monotonic() < deadline:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            chunk = min(max(quiet_ms, 50), left) if left else 0
            if chunk <= 0:
                break
            extra = dom_indexing.settle(handle, quiet_ms=min(50, chunk), max_ms=chunk)
            if extra.get("navigated"):
                return extra
            _network_in_flight(helpers, in_flight)
        return extra
    drain = getattr(helpers, "drain_events", None)
    wait_idle = getattr(helpers, "wait_for_network_idle", None)
    if callable(drain) or not callable(wait_idle) or remaining_ms <= 0:
        return {"quiet": True, "navigated": False}
    try:
        wait_idle(timeout=remaining_ms / 1000.0, idle_ms=float(max(quiet_ms, 1)))
    except TypeError:
        wait_idle(timeout=remaining_ms / 1000.0)
    except Exception:
        pass
    return {"quiet": True, "navigated": False}


def _handle_navigated(handle: tabs.TabHandle) -> None:
    with contextlib.suppress(Exception):
        handle.evaluate(_LOAD_WAIT_JS)
    dom_indexing._register_driver_for_new_documents(handle)


def _settle_post_action(
    handle: tabs.TabHandle, *, quiet_ms: int, max_ms: int
) -> dict[str, Any]:
    started = time.monotonic()
    settled = dom_indexing.settle(handle, quiet_ms=quiet_ms, max_ms=max_ms)
    logger.debug(
        "observe settle js quiet_ms=%s max_ms=%s elapsed_ms=%.0f result=%s",
        quiet_ms,
        max_ms,
        (time.monotonic() - started) * 1000,
        settled,
    )
    if settled.get("navigated"):
        _handle_navigated(handle)
        return {"quiet": True, "navigated": True, "mutations": settled.get("mutations", 0)}
    leftover = _remaining_ms(started, max_ms)
    net = _wait_while_network_busy(handle, remaining_ms=leftover, quiet_ms=quiet_ms)
    logger.debug("observe network leftover_ms=%s result=%s", leftover, net)
    if net.get("navigated"):
        _handle_navigated(handle)
        return {"quiet": True, "navigated": True, "mutations": settled.get("mutations", 0)}
    if not net.get("quiet", True):
        settled = {**settled, "quiet": False}
    return settled


def _url_without_fragment(url: str) -> str:
    return (url or "").split("#", 1)[0]


def _snapshot_tree(
    handle: tabs.TabHandle,
    observe: str,
    *,
    viewport_only: bool | None = None,
    kind: str | None = None,
    contains: str | None = None,
    max_elements: int = 150,
) -> dict[str, Any]:
    viewport = _resolve_viewport_only(viewport_only)
    result = dom_indexing.get_elements(
        handle,
        viewport,
        kind=kind,
        contains=contains,
        max_elements=max_elements,
    )
    if result.get("error"):
        return {"error": result["error"], "observation": {"mode": "full", "tree": ""}, "_lines": []}
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
    navigated = bool(
        handle.state
        and handle.state.last_url
        and url
        and _url_without_fragment(handle.state.last_url) != _url_without_fragment(url)
    )
    previous = handle.state.last_tree if handle.state is not None else None
    full_obs: dict[str, Any] = {
        "mode": "full",
        "url": url,
        "title": title,
        "elementCount": element_count,
        "tree": tree,
        "truncated": truncated,
        "below_viewport": below_viewport,
    }
    use_diff = observe == "diff" and previous is not None and not navigated
    if use_diff:
        diff = dom_indexing.diff_tree_lines(previous, lines)
        added = list(diff["added"])
        removed = list(diff["removed"])
        oversized = (
            len(lines) >= 8
            and len(added) + len(removed) > _DIFF_TO_FULL_RATIO * len(lines)
        )
        if not oversized:
            _cache_tree(handle, url, lines)
            return {
                "url": url,
                "title": title,
                "navigated": navigated,
                "observation": {
                    "mode": "diff",
                    "added": added,
                    "removed": removed,
                    "unchanged": diff["unchanged"],
                    "elementCount": element_count,
                    "url": url,
                    "title": title,
                    "truncated": truncated,
                    "below_viewport": below_viewport,
                },
                "_lines": lines,
            }
    _cache_tree(handle, url, lines)
    full_obs["mode"] = "full"
    return {
        "url": url,
        "title": title,
        "navigated": navigated,
        "observation": full_obs,
        "_lines": lines,
    }


def _observe_after(
    handle: tabs.TabHandle,
    observe: str,
    action: dict[str, Any],
    *,
    before_url: str,
    retry_until_change: bool = False,
) -> dict[str, Any] | None:
    if observe == "none":
        return None
    quiet = _quiet_ms()
    budget = _settle_ms()
    started = time.monotonic()
    before_lines = (
        list(handle.state.last_tree) if handle.state and handle.state.last_tree else None
    )
    logger.debug(
        "observe_after start action=%s observe=%s retry=%s quiet_ms=%s settle_ms=%s "
        "before_url=%s last_tree_n=%s last_url=%s",
        action.get("type"),
        observe,
        retry_until_change,
        quiet,
        budget,
        before_url,
        None if before_lines is None else len(before_lines),
        handle.state.last_url if handle.state else None,
    )
    if retry_until_change and before_lines is None:
        logger.debug(
            "observe_after retry skipped: no last_tree (state=%s)",
            None if handle.state is None else handle.state.target_id,
        )
    settled = _settle_post_action(handle, quiet_ms=quiet, max_ms=budget)
    snap = _snapshot_tree(handle, observe)
    navigated = bool(settled.get("navigated")) or bool(snap.get("navigated"))
    logger.debug(
        "observe_after first snap elapsed_ms=%.0f settled=%s snap_nav=%s mode=%s "
        "url=%s lines=%s changed=%s tree=%r",
        (time.monotonic() - started) * 1000,
        settled,
        snap.get("navigated"),
        (snap.get("observation") or {}).get("mode"),
        snap.get("url"),
        len(snap.get("_lines") or []),
        None if before_lines is None else list(snap.get("_lines") or []) != before_lines,
        "\n".join(snap.get("_lines") or [])[:500],
    )
    retries = 0
    if retry_until_change and before_lines is not None and not navigated:
        while _remaining_ms(started, budget) > 0:
            if list(snap.get("_lines") or []) != before_lines:
                logger.debug(
                    "observe_after retry stop: tree changed after %s extra settles",
                    retries,
                )
                break
            left = _remaining_ms(started, budget)
            logger.debug("observe_after retry n=%s left_ms=%s", retries, left)
            tick = time.monotonic()
            extra = _settle_post_action(
                handle, quiet_ms=min(quiet, left) if quiet else left, max_ms=left
            )
            if (time.monotonic() - tick) * 1000 < 20 and left > 0:
                time.sleep(min(max(quiet, 1), left) / 1000.0)
            retries += 1
            if extra.get("navigated"):
                settled = extra
                navigated = True
                snap = _snapshot_tree(handle, observe)
                logger.debug(
                    "observe_after retry navigated extra=%s url=%s",
                    extra,
                    snap.get("url"),
                )
                break
            snap = _snapshot_tree(handle, observe)
            logger.debug(
                "observe_after retry snap n=%s nav=%s mode=%s url=%s changed=%s tree=%r",
                retries,
                snap.get("navigated"),
                (snap.get("observation") or {}).get("mode"),
                snap.get("url"),
                list(snap.get("_lines") or []) != before_lines,
                "\n".join(snap.get("_lines") or [])[:500],
            )
            if snap.get("navigated"):
                navigated = True
                break
            settled = extra
    if before_url and snap.get("url"):
        if _url_without_fragment(before_url) != _url_without_fragment(str(snap["url"])):
            navigated = True
            logger.debug(
                "observe_after url changed before=%s after=%s", before_url, snap.get("url")
            )
    observation = dict(snap.get("observation") or {"mode": "full", "tree": ""})
    quiet_flag = bool(settled.get("quiet", True))
    if not quiet_flag:
        observation["settled"] = False
    logger.debug(
        "observe_after done elapsed_ms=%.0f retries=%s navigated=%s settled=%s mode=%s",
        (time.monotonic() - started) * 1000,
        retries,
        navigated,
        quiet_flag,
        observation.get("mode"),
    )
    return {
        "action": action,
        "page": {
            "url": str(snap.get("url") or ""),
            "title": str(snap.get("title") or ""),
            "navigated": navigated,
            "settled": quiet_flag,
        },
        "observation": observation,
    }


def _with_observation(
    payload: dict[str, Any], wrapped: dict[str, Any] | None
) -> dict[str, Any]:
    if wrapped is None:
        return payload
    return {**payload, **wrapped}


def _unknown_tab_result(exc: tabs.UnknownTabError) -> str:
    return _json_text({"ok": False, "error": str(exc)})


def _focused_handle(helpers: Any) -> tabs.TabHandle:
    """Resolve the focused tab, refreshing the registry when it has no focus yet."""
    reg = tabs.registry()
    focused = reg.focused()
    refreshed = False
    if focused is None:
        logger.debug("focused_handle: registry has no focus; refreshing")
        reg.refresh(helpers)
        focused = reg.focused()
        refreshed = True
    if focused is not None:
        logger.debug(
            "focused_handle target=%s last_tree_n=%s last_url=%s refreshed=%s",
            focused.target_id,
            None if focused.last_tree is None else len(focused.last_tree),
            focused.last_url,
            refreshed,
        )
        reg.mark_used(focused)
        return reg.focused_handle(helpers, focused)
    logger.debug("focused_handle: still no TabState after refresh=%s", refreshed)
    return tabs.TabHandle(helpers, None, focused=True)


def _for_read(helpers: Any, tab: str | None) -> tabs.TabHandle:
    """Resolve a tab for a read. Never calls switch_tab."""
    if tab is None:
        return _focused_handle(helpers)
    reg = tabs.registry()
    reg.refresh(helpers)
    state = reg.resolve(tab)
    focused = state.target_id == reg.focused_id
    handle = reg.handle(helpers, state, focused=focused)
    reg.mark_used(state)
    return handle


def _for_action(helpers: Any, tab: str | None) -> tabs.TabHandle:
    """Resolve a tab for an action. Focuses it first when it is not already focused."""
    if tab is None:
        return _focused_handle(helpers)
    reg = tabs.registry()
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


def _goto_observation(
    handle: tabs.TabHandle, url: str, observe: str, payload: dict[str, Any]
) -> str:
    wrapped = _observe_after(
        handle, observe, {"type": "goto", "url": url}, before_url="", retry_until_change=False
    )
    return _json_text(_with_observation(payload, wrapped))


def _playbook_hints(url: str | None) -> dict[str, Any]:
    key = url or ""
    try:
        return {
            "playbooks": playbooks.list_playbook_names(key) if key else playbooks.list_playbook_names(),
            "flows": playbooks.list_flows(key) if key else playbooks.list_flows(),
        }
    except playbooks.PlaybookError:
        return {"playbooks": [], "flows": []}


def _playbook_timeout_s() -> float:
    raw = (os.environ.get("BROWSER_MCP_PLAYBOOK_TIMEOUT_S") or "").strip()
    if not raw:
        return 120.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 120.0


def _act_context(helpers: Any, handle: tabs.TabHandle) -> actions.ActContext:
    def _open(url: str, *, alias: str | None = None, focus: bool = False) -> dict[str, Any]:
        return _open_tab(helpers, url, alias=alias, focus=focus)

    return actions.ActContext(
        helpers=helpers,
        handle=handle,
        for_action=lambda name: _for_action(helpers, name),
        open_tab=_open,
        login=_sealed_login,
    )


@mcp.tool()
@_public_tool
def browser_goto(
    url: str, new_tab: bool = False, observe: str | None = None, tab: str | None = None
) -> str:
    """Navigate to a URL in the current tab (or a new tab if new_tab=True / current is blank).

    Pass tab= to navigate a specific tab in place without focusing it. new_tab=True
    opens a new tab and focuses it (same as browser_open_tab with focus=True).

    Returns page info, matching playbook filenames and executable flows, and a
    full observation by default (observe=\"diff\" / \"none\" to change that).
    """
    mode = _resolve_action_observe(observe, default="full")
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
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
        handle = _for_action(helpers, opened.get("tab"))
        return _goto_observation(handle, url, mode, {**info, **_playbook_hints(url)})
    if tab is not None:
        try:
            handle = _for_read(helpers, tab)
        except tabs.UnknownTabError as exc:
            return _unknown_tab_result(exc)
        result = actions.do_goto(handle, url)
        return _goto_observation(handle, url, mode, {**result, **_playbook_hints(url)})
    actions.do_goto(_for_action(helpers, None), url)
    handle = _for_action(helpers, None)
    info = handle.page_info()
    return _goto_observation(handle, url, mode, {**info, **_playbook_hints(url)})


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
        return _observe_error(observe, _OBSERVE_MODES)
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
    snap = _snapshot_tree(
        handle,
        observe_norm,
        viewport_only=viewport,
        kind=kind_norm,
        contains=contains,
        max_elements=cap,
    )
    if snap.get("error"):
        return _json_text({"ok": False, "error": snap["error"]})
    payload = {"ok": True, **snap["observation"]}
    if observe_norm != "diff":
        payload.pop("mode", None)
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
def browser_click_by_index(
    index: int, observe: str | None = None, tab: str | None = None
) -> str:
    """Click an interactive element by index from browser_get_elements. Prefer this over
    browser_click(x, y) — no pixel-guessing, resilient to layout shifts.

    Default observe=\"diff\" returns the settled page snapshot in the response.
    """
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    try:
        payload = actions.do_click_by_index(handle, index)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    action: dict[str, Any] = {"type": "click", "index": index, "clicked": payload["clicked"]}
    if payload.get("warning"):
        action["warning"] = payload["warning"]
    wrapped = _observe_after(
        handle, mode, action, before_url=before_url, retry_until_change=True
    )
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_input_by_index(
    index: int,
    text: str,
    clear_first: bool = True,
    mode: str = "auto",
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Click and type text into an indexed input/textarea/contenteditable element from
    browser_get_elements. Prefer this over screenshot + coordinate typing.

    mode: ``auto`` (default) tries an in-page value set and falls back to real key
    events; ``keys`` always types; ``fast`` never falls back. Pass ``keys`` for
    comboboxes and fields that only listen to keydown. Override the default with
    BROWSER_MCP_FILL_MODE. Default observe=\"diff\" returns the settled snapshot.
    """
    observe_mode = _resolve_action_observe(observe)
    if observe_mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(
            observe if observe is not None else observe_mode, _ACTION_OBSERVE_MODES
        )
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    try:
        payload = actions.do_input_by_index(
            handle, index, text, clear_first=clear_first, mode=mode
        )
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    wrapped = _observe_after(
        handle,
        observe_mode,
        {
            "type": "input",
            "index": index,
            "tagName": payload.get("tagName"),
            "mode_used": payload.get("mode_used"),
        },
        before_url=str(handle.page_info().get("url") or ""),
    )
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_select_by_index(
    index: int, text: str, observe: str | None = None, tab: str | None = None
) -> str:
    """Select a <select> dropdown option by visible text, using the index from
    browser_get_elements."""
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    try:
        payload = actions.do_select_by_index(handle, index, text)
    except dom_indexing.ElementNotFoundError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "select", "index": index, "selected": text},
        before_url=str(handle.page_info().get("url") or ""),
    )
    return _json_text(_with_observation(payload, wrapped))


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
    x: float,
    y: float,
    button: str = "left",
    clicks: int = 1,
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Click at viewport coordinates (x, y).

    LAST-RESORT FALLBACK: prefer browser_get_elements + browser_click_by_index for
    ordinary clicking. Only use raw coordinates for elements browser_get_elements can't
    represent (canvas, shadow DOM, drag targets) or after visually confirming a spot via
    browser_screenshot.
    """
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    actions.do_click_xy(handle, x, y, button=button, clicks=clicks)
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "click", "x": x, "y": y, "button": button, "clicks": clicks},
        before_url=before_url,
        retry_until_change=True,
    )
    return _json_text(_with_observation({"ok": True}, wrapped))


@mcp.tool()
@_public_tool
def browser_fill(
    selector: str,
    text: str,
    clear_first: bool = True,
    timeout: float = 0.0,
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Fill a form input (works with React/Vue controlled inputs)."""
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    actions.do_fill_selector(
        handle, selector, text, clear_first=clear_first, timeout=timeout
    )
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "fill", "selector": selector},
        before_url=str(handle.page_info().get("url") or ""),
    )
    return _json_text(_with_observation({"ok": True}, wrapped))


@mcp.tool()
@_public_tool
def browser_press_key(
    key: str, modifiers: int = 0, observe: str | None = None, tab: str | None = None
) -> str:
    """Press a key. Modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift."""
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    actions.do_press(handle, key, modifiers)
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "press", "key": key, "modifiers": modifiers},
        before_url=str(handle.page_info().get("url") or ""),
        retry_until_change=True,
    )
    return _json_text(_with_observation({"ok": True}, wrapped))


@mcp.tool()
@_public_tool
def browser_scroll(
    x: float,
    y: float,
    dy: float = -300,
    dx: float = 0,
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Scroll the page at viewport position (x, y)."""
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    actions.do_scroll(handle, x, y, dy=dy, dx=dx)
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "scroll", "x": x, "y": y, "dy": dy, "dx": dx},
        before_url=str(handle.page_info().get("url") or ""),
        retry_until_change=True,
    )
    return _json_text(_with_observation({"ok": True}, wrapped))


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
    return _json_text(
        actions.do_wait_for(handle, selector, visible=visible, timeout=timeout)
    )


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
    return _json_text(actions.do_wait_idle(handle, timeout=timeout, idle_ms=idle_ms))


@mcp.tool()
@_public_tool
def browser_act(
    steps: list[dict[str, Any]],
    observe: str | None = None,
    stop_on_error: bool = True,
    tab: str | None = None,
) -> str:
    """Run up to 25 sequential browser steps in one turn.

    Each step is ``{do, ...}``. Allowed do values: click, input, select, press,
    click_text, wait_for, wait_idle, goto, scroll, settle, tab, open_tab,
    fill_form, login. After each action the page settles; one observation is
    returned at the end for the focused tab. On the first failure
    (stop_on_error=True, default) returns completed steps, failed_step, error,
    and the current observation so you can resume. login maps to browser_login
    (user-focused tab; always pass expected_origin).
    """
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    validated = actions.validate_steps(steps)
    if isinstance(validated, dict):
        return _json_text(validated)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)

    ctx = _act_context(helpers, handle)
    before_url = str(handle.page_info().get("url") or "")
    executed = actions.execute_steps(ctx, validated, stop_on_error=stop_on_error)
    handle = executed.pop("handle", ctx.handle)
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "act", "steps": len(validated)},
        before_url=before_url,
    )
    payload = {k: v for k, v in executed.items() if k != "handle"}
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_fill_form(
    fields: dict[str, str],
    submit: bool = False,
    mode: str = "auto",
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Fill a form by field label in one call.

    Resolves each key with label[for], aria-label, aria-labelledby, placeholder,
    name, id, then nearest preceding row text (case-insensitive, unique
    substring). Selects for <select>; checks/unchecks checkboxes when the value
    is \"true\"/\"false\". Unresolved labels are listed and are not an error
    unless every field failed. submit=True clicks the form's submit button if
    one exists and is enabled, otherwise presses Enter in the last field.
    The ``how`` field on each filled entry says which strategy matched.
    """
    observe_mode = _resolve_action_observe(observe)
    if observe_mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(
            observe if observe is not None else observe_mode, _ACTION_OBSERVE_MODES
        )
    if not isinstance(fields, dict):
        return _json_text({"ok": False, "error": "fields must be an object of label → value"})
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    payload = actions.do_fill_form(handle, fields, submit=submit, mode=mode)
    wrapped = _observe_after(
        handle,
        observe_mode,
        {"type": "fill_form", "filled": len(payload.get("filled") or [])},
        before_url=before_url,
    )
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_click_text(
    text: str,
    role: str | None = None,
    exact: bool = False,
    nth: int = 0,
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Click the interactive element whose visible text or aria-label matches.

    role restricts to button|link|tab|menuitem|checkbox|radio|option. exact
    toggles equality vs substring. Prefers visible, in-viewport, top elements.
    On a miss, returns did_you_mean with up to five near-misses.
    """
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    if role is not None and str(role).strip():
        role_norm = str(role).strip().lower()
        if role_norm not in actions.CLICK_TEXT_ROLES:
            names = ", ".join(sorted(actions.CLICK_TEXT_ROLES))
            return _json_text({"ok": False, "error": f"unknown role {role!r}; expected {names}"})
    else:
        role_norm = None
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    payload = actions.do_click_text(
        handle, text, role=role_norm, exact=exact, nth=int(nth)
    )
    if not payload.get("ok"):
        return _json_text(payload)
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "click_text", "text": text, "index": payload.get("index")},
        before_url=before_url,
        retry_until_change=True,
    )
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_extract(
    selector: str,
    fields: dict[str, str],
    limit: int = 50,
    tab: str | None = None,
) -> str:
    """Extract structured rows from elements matching ``selector``.

    Each field is a relative sub-selector (``\"title\": \"h2\"``). Append
    ``@attr`` for an attribute (``\"href\": \"a@href\"``). Missing nodes are
    null. Use this instead of ad-hoc ``browser_js`` scraping.
    """
    if not isinstance(fields, dict) or not fields:
        return _json_text({"ok": False, "error": "fields must be a non-empty object of name → selector"})
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        return _json_text({"ok": False, "error": "limit must be an integer"})
    helpers, _ = _browser_harness()
    try:
        handle = _for_read(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    result = dom_indexing.extract_rows(handle, selector, fields, limit=cap)
    if result.get("error"):
        return _json_text({"ok": False, "error": result["error"]})
    return _json_text(
        {
            "ok": True,
            "rows": result.get("rows") or [],
            "truncated": bool(result.get("truncated")),
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
def browser_switch_tab(target_id: str, observe: str | None = None) -> str:
    """Switch to a tab by alias or target_id (from browser_tabs)."""
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, target_id)
    except tabs.UnknownTabError:
        sid = helpers.switch_tab(target_id)
        dom_indexing.mark_driver_stale()
        dom_indexing._register_driver_for_new_documents(helpers)
        handle = _for_action(helpers, None)
        payload = {"ok": True, "session_id": sid}
        wrapped = _observe_after(
            handle,
            mode,
            {"type": "switch_tab", "tab": target_id},
            before_url=str(handle.page_info().get("url") or ""),
            retry_until_change=True,
        )
        return _json_text(_with_observation(payload, wrapped))
    sid = handle.switch_session_id
    if sid is None and handle.state is not None:
        sid = helpers.switch_tab(handle.state.target_id)
    dom_indexing._register_driver_for_new_documents(handle)
    payload = {"ok": True, "session_id": sid}
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "switch_tab", "tab": target_id},
        before_url=str(handle.page_info().get("url") or ""),
        retry_until_change=True,
    )
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_open_tab(
    url: str,
    alias: str | None = None,
    focus: bool = False,
    observe: str | None = None,
) -> str:
    """Open a URL in a new tab. Defaults to the background (does not steal focus).

    alias must match [a-z][a-z0-9_-]{0,23}. At most five agent-controlled tabs;
    on the cap this returns tab_limit_reached and does not open or close anything.
    When focus=True, returns a full observation by default.
    """
    helpers, _ = _browser_harness()
    try:
        opened = _open_tab(helpers, url, alias=alias, focus=focus)
    except (ValueError, tabs.UnknownTabError) as exc:
        return _json_text({"ok": False, "error": str(exc)})
    if not opened.get("ok") or not focus:
        return _json_text(opened)
    mode = _resolve_action_observe(observe, default="full")
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    handle = _for_action(helpers, str(opened.get("tab") or ""))
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "open_tab", "url": url, "tab": opened.get("tab")},
        before_url="",
    )
    return _json_text(_with_observation(opened, wrapped))


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
    """List playbook markdown filenames and executable flows, optionally filtered by host."""
    try:
        hints = _playbook_hints(host)
    except playbooks.PlaybookError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text(
        {
            "ok": True,
            "playbooks_dir": str(playbooks.playbooks_dir()),
            **hints,
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
    """Write or append a site playbook. ```playbook fences are validated before save."""
    try:
        result = playbooks.write_playbook(host, content, append=append)
    except playbooks.PlaybookError as exc:
        logger.warning("browser_write_playbook failed for host=%r: %s", host, exc)
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text(result)


@mcp.tool()
@_public_tool
def browser_run_playbook(
    host: str,
    name: str,
    params: dict[str, str] | None = None,
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Execute a named ```playbook flow from the host markdown file.

    Substitutes {{param}} into string fields, runs steps via browser_act, then
    checks expect (url_contains, selector, text). On failure returns failed_step
    plus an observation so you can continue by hand. Secrets are never params —
    use {do: login, expected_origin: ...} which maps to browser_login (the
    user-focused tab). Capped by BROWSER_MCP_PLAYBOOK_TIMEOUT_S (default 120).
    """
    mode = _resolve_action_observe(observe)
    if mode not in _ACTION_OBSERVE_MODES:
        return _observe_error(observe if observe is not None else mode, _ACTION_OBSERVE_MODES)
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _json_text({"ok": False, "error": "params must be an object"})
    try:
        flow = playbooks.load_flow(host, name)
        steps = playbooks.substitute_params(flow, params)
    except playbooks.PlaybookError as exc:
        return _json_text({"ok": False, "error": str(exc), "name": name})
    helpers, _ = _browser_harness()
    try:
        handle = _for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return _unknown_tab_result(exc)
    ctx = _act_context(helpers, handle)
    before_url = str(handle.page_info().get("url") or "")
    deadline = time.monotonic() + _playbook_timeout_s()
    executed = actions.execute_steps(ctx, steps, stop_on_error=True, deadline=deadline)
    handle = executed.pop("handle", ctx.handle)
    wrapped = _observe_after(
        handle,
        mode,
        {"type": "run_playbook", "name": name, "steps": len(steps)},
        before_url=before_url,
    )
    payload = {k: v for k, v in executed.items() if k != "handle"}
    payload["name"] = name
    if payload.get("ok"):
        payload["completed"] = payload.pop("steps", [])
        expect_error = playbooks.check_expect(handle, flow.expect)
        if expect_error:
            payload["ok"] = False
            payload["error"] = expect_error
    return _json_text(_with_observation(payload, wrapped))


@mcp.tool()
@_public_tool
def browser_recent_actions(host: str) -> str:
    """Return the last 50 successful actions for a host (labels and lengths, not typed text)."""
    try:
        slug = playbooks.host_slug(host)
    except playbooks.PlaybookError as exc:
        return _json_text({"ok": False, "error": str(exc)})
    return _json_text(
        {
            "ok": True,
            "host": slug,
            "actions": tabs.registry().recent_actions(slug),
        }
    )


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
