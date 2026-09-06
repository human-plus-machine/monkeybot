"""FastMCP instance, tool-lock, and the public-error wrapper."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec

from mcp.server.fastmcp import FastMCP

from browser_mcp import backend, in_app_cdp, perf, results, tab_ops, tabs
from browser_mcp.observe import resolve_action_observe

_P = ParamSpec("_P")
_TOOL_LOCK = threading.RLock()

mcp = FastMCP(
    "browser",
    instructions=(
        "Real-browser control via CDP (browser-harness). Use browser_* tools for web tasks.\n"
        "\n"
        "Default workflow — text-based, indexed DOM interaction:\n"
        "1. Prefer intent tools when you already know the labels or the multi-step "
        "flow: browser_click_text when the visible label is known, browser_act for a "
        "batch of clicks/inputs/waits/fill_form (cap 25 steps), browser_extract for "
        "structured scraping instead of browser_js.\n"
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
        "If the user asked to sign in on the Spaces in-app browser and a saved "
        "password exists, call browser_login(expected_origin=...) — never read or "
        "type the password yourself, and check the returned origin. If "
        "browser_login returns 'login needs your attention', tell the user to "
        "finish signing in themselves in the Spaces browser; do not retry and do "
        "not attempt to type a password. "
        "Call browser_stop when done with remote/cloud sessions.\n"
        "\n"
        "Tabs: each tab has a short alias (t1, t2, …) or a name you pass to "
        "browser_open_tab(alias=...). Reads (get_elements, get_text, page_info, js, wait_for, "
        "read_tabs) never move focus — pass tab= to address a background tab. "
        "Actions (click, input, select, fill, click_text, act, screenshot, …) focus the tab first "
        "because background tabs throttle timers and pause painting. Open a second "
        "tab to compare pages, keep a form while reading docs, or fan out with "
        "browser_read_tabs. At most five agent-controlled tabs; if you hit the cap, "
        "relay the returned tab list to the user, ask which to close, then "
        "browser_close_tab and retry — never close a tab without their confirmation. "
        "Close tabs you opened when done. Do not expect a background SPA to finish "
        "loading while unfocused."
    ),
)


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
                    in_app_cdp._reraise_public_harness_error(exc)

    return wrapper


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """Shared result of the per-tool observe/harness/tab preamble."""

    error: str | None
    helpers: Any = None
    handle: Any = None
    mode: str = "diff"
    before_url: str = ""


def observe_mode(observe: str | None, *, default: str = "diff") -> tuple[str, str | None]:
    """Return (mode, error_json). error_json is set when observe is unknown."""
    mode = resolve_action_observe(observe, default=default)
    if mode not in results.ACTION_OBSERVE_MODES:
        return mode, results.observe_error(
            observe if observe is not None else mode, results.ACTION_OBSERVE_MODES
        )
    return mode, None


def prepare_action(
    tab: str | None = None,
    *,
    observe: str | None = None,
    default: str | None = None,
    focus: bool = False,
    capture_url: bool = False,
) -> PreparedAction:
    """Bind the backend and resolve a tab. Pass ``default`` to validate observe mode."""
    mode = "diff"
    if default is not None:
        mode, error = observe_mode(observe, default=default)
        if error:
            return PreparedAction(error=error, mode=mode)
    helpers, _ = backend.browser_harness()
    try:
        handle = (
            tab_ops._for_action(helpers, tab) if focus else tab_ops._for_read(helpers, tab)
        )
    except tabs.UnknownTabError as exc:
        return PreparedAction(error=results.unknown_tab_result(exc), mode=mode)
    before_url = str(handle.page_info().get("url") or "") if capture_url else ""
    return PreparedAction(
        error=None, helpers=helpers, handle=handle, mode=mode, before_url=before_url
    )
