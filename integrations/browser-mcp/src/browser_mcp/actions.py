"""Shared action bodies for single tools and ``browser_act``.

Tool wrappers in ``server.py`` resolve a tab, call these helpers, then attach
an observation. ``browser_act`` runs the same helpers in a loop.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from browser_mcp import dom_indexing
from browser_mcp.tabs import TabHandle

logger = logging.getLogger(__name__)

MAX_ACT_STEPS = 25
FILL_MODES = frozenset({"auto", "keys", "fast"})
CLICK_TEXT_ROLES = frozenset(
    {"button", "link", "tab", "menuitem", "checkbox", "radio", "option"}
)
STEP_KINDS = frozenset(
    {
        "click",
        "input",
        "select",
        "press",
        "click_text",
        "wait_for",
        "wait_idle",
        "goto",
        "scroll",
        "settle",
        "tab",
        "open_tab",
        "fill_form",
        "login",
    }
)
SKIP_SETTLE = frozenset({"wait_for", "wait_idle", "settle", "goto", "open_tab", "login"})
PLAYBOOK_TIMEOUT_ERROR = "playbook timeout"
LOAD_WAIT_JS = (
    "document.readyState==='complete' ? true : new Promise((resolve) => {"
    "const t = setTimeout(() => resolve(false), 15000);"
    "addEventListener('load', () => { clearTimeout(t); resolve(true); }, {once:true});"
    "})"
)
WAIT_IDLE_NOTE = (
    "network idle is only available on the focused tab; DOM settle was used"
)


@dataclass
class ActContext:
    """Mutable handle plus tab callbacks so the executor can switch tabs."""

    helpers: Any
    handle: TabHandle
    for_action: Callable[[str | None], TabHandle]
    open_tab: Callable[..., dict[str, Any]]
    login: Callable[..., dict[str, Any]]


def _is_blank_url(url: str) -> bool:
    url = url or ""
    return url in ("", "about:blank") or url.startswith("about:blank#")


def can_goto_in_place(helpers: Any) -> bool:
    if not callable(getattr(helpers, "goto_url", None)):
        return False
    url = ""
    if callable(getattr(helpers, "current_tab", None)):
        try:
            tab = helpers.current_tab()
        except Exception:
            logger.debug("current_tab failed in can_goto_in_place", exc_info=True)
            tab = None
        if isinstance(tab, dict):
            url = str(tab.get("url") or "")
    if not url and callable(getattr(helpers, "page_info", None)):
        try:
            info = helpers.page_info() or {}
            url = str(info.get("url") or "")
        except Exception:
            logger.debug("page_info failed in can_goto_in_place", exc_info=True)
            url = ""
    return not _is_blank_url(url)


def resolve_fill_mode(mode: str) -> str:
    raw = (mode or "auto").strip().lower()
    if raw == "auto":
        raw = (os.environ.get("BROWSER_MCP_FILL_MODE") or "auto").strip().lower()
    return raw if raw in FILL_MODES else "auto"


def do_click_by_index(handle: TabHandle, index: int) -> dict[str, Any]:
    rect = dom_indexing.get_rect(handle, index)
    handle.helpers.click_at_xy(rect["x"], rect["y"])
    payload: dict[str, Any] = {"ok": True, "clicked": rect}
    obscured = rect.get("obscuredBy")
    if obscured:
        payload["warning"] = f"target obscured by {obscured}"
    return payload


def do_input_by_index(
    handle: TabHandle,
    index: int,
    text: str,
    *,
    clear_first: bool = True,
    mode: str = "auto",
) -> dict[str, Any]:
    result = dom_indexing.fill(
        handle, index, text, clear_first=clear_first, mode=resolve_fill_mode(mode)
    )
    return {
        "ok": True,
        "index": index,
        "tagName": result.get("tagName"),
        "mode_used": result.get("mode_used"),
    }


def do_select_by_index(handle: TabHandle, index: int, text: str) -> dict[str, Any]:
    dom_indexing.select_option(handle, index, text)
    return {"ok": True, "index": index, "selected": text}


def do_click_xy(
    handle: TabHandle,
    x: float,
    y: float,
    *,
    button: str = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    handle.helpers.click_at_xy(x, y, button=button, clicks=clicks)
    return {"ok": True, "x": x, "y": y, "button": button, "clicks": clicks}


def do_fill_selector(
    handle: TabHandle,
    selector: str,
    text: str,
    *,
    clear_first: bool = True,
    timeout: float = 0.0,
) -> dict[str, Any]:
    handle.helpers.fill_input(selector, text, clear_first=clear_first, timeout=timeout)
    return {"ok": True, "selector": selector}


def do_press(handle: TabHandle, key: str, modifiers: int = 0) -> dict[str, Any]:
    handle.helpers.press_key(key, modifiers=modifiers)
    return {"ok": True, "key": key, "modifiers": modifiers}


def do_scroll(
    handle: TabHandle, x: float, y: float, *, dy: float = -300, dx: float = 0
) -> dict[str, Any]:
    handle.helpers.scroll(x, y, dy=dy, dx=dx)
    return {"ok": True, "x": x, "y": y, "dy": dy, "dx": dx}


def do_wait_for(
    handle: TabHandle, selector: str, *, visible: bool = False, timeout: float = 10.0
) -> dict[str, Any]:
    result = dom_indexing.wait_for_selector(
        handle, selector, visible=visible, timeout=timeout
    )
    found = bool(result.get("found"))
    return {"ok": found, "found": found}


def _settle_idle_note(handle: TabHandle, note: str) -> dict[str, Any]:
    settled = dom_indexing.settle(handle)
    return {
        "ok": True,
        "idle": None,
        "quiet": bool(settled.get("quiet", True)),
        "navigated": bool(settled.get("navigated", False)),
        "note": note,
    }


def do_wait_idle(
    handle: TabHandle, *, timeout: float = 10.0, idle_ms: float = 500
) -> dict[str, Any]:
    helpers = handle.helpers
    if not handle.focused:
        return _settle_idle_note(handle, WAIT_IDLE_NOTE)
    wait_idle = getattr(helpers, "wait_for_network_idle", None)
    if not callable(wait_idle):
        return _settle_idle_note(
            handle, "network idle is not available on this backend; DOM settle was used"
        )
    idle = bool(wait_idle(timeout=timeout, idle_ms=idle_ms))
    if not idle:
        return {"ok": False, "idle": False, "quiet": False, "navigated": False}
    settled = dom_indexing.settle(handle)
    return {
        "ok": True,
        "idle": True,
        "quiet": bool(settled.get("quiet", True)),
        "navigated": bool(settled.get("navigated", False)),
    }


def do_click_text(
    handle: TabHandle,
    text: str,
    *,
    role: str | None = None,
    exact: bool = False,
    nth: int = 0,
) -> dict[str, Any]:
    found = dom_indexing.find_by_text(handle, text, role=role, exact=exact, nth=nth)
    if not found.get("ok"):
        return {
            "ok": False,
            "error": f"no matching element for {text!r}",
            "did_you_mean": found.get("nearMisses") or [],
        }
    handle.helpers.click_at_xy(found["x"], found["y"])
    payload: dict[str, Any] = {
        "ok": True,
        "index": found.get("index"),
        "clicked": found,
    }
    obscured = found.get("obscuredBy")
    if obscured:
        payload["warning"] = f"target obscured by {obscured}"
    return payload


def do_goto(handle: TabHandle, url: str) -> dict[str, Any]:
    helpers = handle.helpers
    if handle.focused and not can_goto_in_place(helpers):
        helpers.new_tab(url)
        dom_indexing.mark_driver_stale()
        target: Any = helpers
    elif handle.focused:
        handle.navigate(url)
        target = helpers
    else:
        handle.navigate(url)
        target = handle
    if isinstance(target, TabHandle):
        target.evaluate(LOAD_WAIT_JS)
        dom_indexing._register_driver_for_new_documents(target)
        dom_indexing.settle(target)
        info = target.page_info()
    else:
        helpers.js(LOAD_WAIT_JS)
        dom_indexing._register_driver_for_new_documents(helpers)
        dom_indexing.settle(helpers)
        info = helpers.page_info() if callable(getattr(helpers, "page_info", None)) else {}
        if not isinstance(info, dict):
            info = {}
    url = str(info.get("url") or "")
    if handle.state is not None and url:
        handle.state.url = url
        handle.state.title = str(info.get("title") or handle.state.title)
    return {
        "ok": True,
        "url": info.get("url"),
        "title": info.get("title"),
    }


def do_settle(handle: TabHandle) -> dict[str, Any]:
    result = dom_indexing.settle(handle)
    return {
        "ok": True,
        "quiet": bool(result.get("quiet", True)),
        "navigated": bool(result.get("navigated", False)),
        "mutations": result.get("mutations", 0),
    }


def _fill_one_field(
    handle: TabHandle, label: str, value: str, mode: str
) -> dict[str, Any] | None:
    resolved = dom_indexing.resolve_field(handle, label)
    index = resolved.get("index")
    if index is None:
        return None
    idx = int(index)
    tag = str(resolved.get("tagName") or "").lower()
    typ = str(resolved.get("type") or "").lower()
    try:
        if tag == "select":
            do_select_by_index(handle, idx, value)
        elif tag == "input" and typ in {"checkbox", "radio"}:
            flag = value.strip().lower()
            if flag not in {"true", "false"}:
                return None
            dom_indexing.set_checked(handle, idx, flag == "true")
        else:
            do_input_by_index(handle, idx, value, mode=mode)
    except (dom_indexing.ElementNotFoundError, RuntimeError):
        logger.debug("fill_form field %r failed", label, exc_info=True)
        return None
    return {"label": label, "index": idx, "how": resolved.get("how")}


def do_fill_form(
    handle: TabHandle,
    fields: dict[str, str],
    *,
    submit: bool = False,
    mode: str = "auto",
) -> dict[str, Any]:
    filled: list[dict[str, Any]] = []
    unresolved: list[str] = []
    last_index: int | None = None
    for label, value in fields.items():
        row = _fill_one_field(handle, str(label), str(value), mode)
        if row is None:
            unresolved.append(str(label))
            continue
        filled.append(row)
        last_index = int(row["index"])
    submitted = False
    if submit and last_index is not None:
        submitted = _submit_form(handle, last_index)
    ok = bool(filled) or not fields
    if fields and not filled:
        ok = False
    return {
        "ok": ok,
        "filled": filled,
        "unresolved": unresolved,
        "submitted": submitted,
    }


def _submit_form(handle: TabHandle, last_index: int) -> bool:
    found = dom_indexing.find_form_submit(handle, last_index)
    if found.get("ok") and not found.get("disabled"):
        handle.helpers.click_at_xy(found["x"], found["y"])
        return True
    do_press(handle, "Enter", 0)
    return True


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _step_error(index: int, message: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "step_index": index}


def validate_steps(steps: Any) -> dict[str, Any] | list[dict[str, Any]]:
    """Return normalized steps, or ``{ok:false, error, step_index}``."""
    if not isinstance(steps, list):
        return _step_error(0, "steps must be a list of objects")
    if len(steps) > MAX_ACT_STEPS:
        return _step_error(
            MAX_ACT_STEPS, f"too many steps ({len(steps)}); cap is {MAX_ACT_STEPS}"
        )
    if not steps:
        return _step_error(0, "steps must not be empty")
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            return _step_error(i, "each step must be an object")
        kind = raw.get("do")
        if not isinstance(kind, str) or kind not in STEP_KINDS:
            known = ", ".join(sorted(STEP_KINDS))
            return _step_error(i, f"unknown do {kind!r}; expected {known}")
        err = _validate_step(kind, raw)
        if err:
            return _step_error(i, err)
        out.append(dict(raw))
    return out


def _validate_step(kind: str, step: dict[str, Any]) -> str | None:
    validator = _STEP_VALIDATORS.get(kind)
    return validator(step) if validator else None


def _fail_payload(
    completed: list[dict[str, Any]],
    index: int,
    error: str | None,
    handle: TabHandle,
    extra: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "completed": completed,
        "failed_step": index,
        "error": error,
        "handle": handle,
    }
    out.update(extra)
    return out


def execute_steps(
    ctx: ActContext,
    steps: list[dict[str, Any]],
    *,
    stop_on_error: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
    failed_step: int | None = None
    error: str | None = None
    for i, step in enumerate(steps):
        if deadline is not None and time.monotonic() >= deadline:
            return _fail_payload(completed, i, PLAYBOOK_TIMEOUT_ERROR, ctx.handle, extra)
        try:
            result = _run_step(ctx, step)
        except dom_indexing.ElementNotFoundError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("browser step %s (%s) failed", i, step.get("do"))
            result = {"ok": False, "error": str(exc)}
        row = {**result, "do": step.get("do"), "step_index": i}
        if result.get("ok"):
            completed.append(row)
            if step.get("do") not in SKIP_SETTLE:
                dom_indexing.settle(ctx.handle)
            continue
        failed_step = i
        error = str(result.get("error") or "step failed")
        if result.get("did_you_mean") is not None:
            extra["did_you_mean"] = result.get("did_you_mean")
        if stop_on_error:
            return _fail_payload(completed, i, error, ctx.handle, extra)
        completed.append(row)
    if failed_step is not None:
        ok_rows = [row for row in completed if row.get("ok")]
        return _fail_payload(ok_rows, failed_step, error, ctx.handle, extra)
    return {"ok": True, "steps": completed, "handle": ctx.handle}


def _run_step(ctx: ActContext, step: dict[str, Any]) -> dict[str, Any]:
    runner = _STEP_RUNNERS.get(str(step["do"]))
    if runner is None:
        return {"ok": False, "error": f"unknown do {step['do']!r}"}
    return runner(ctx, step)


def _run_goto(ctx: ActContext, step: dict[str, Any]) -> dict[str, Any]:
    result = do_goto(ctx.handle, str(step["url"]))
    if ctx.handle.focused:
        ctx.handle = ctx.for_action(None)
    return result


def _run_tab(ctx: ActContext, step: dict[str, Any]) -> dict[str, Any]:
    ctx.handle = ctx.for_action(str(step["tab"]))
    return {"ok": True, "tab": step["tab"]}


def _run_open_tab(ctx: ActContext, step: dict[str, Any]) -> dict[str, Any]:
    opened = ctx.open_tab(
        str(step["url"]),
        alias=step.get("alias"),
        focus=bool(step.get("focus", False)),
    )
    if opened.get("ok") and opened.get("focused"):
        ctx.handle = ctx.for_action(str(opened.get("tab") or ""))
    return opened


def _req_int(step: dict[str, Any], key: str, label: str) -> str | None:
    if not _is_int(step.get(key)):
        return label
    return None


def _req_str(step: dict[str, Any], key: str, label: str) -> str | None:
    if not _is_str(step.get(key)):
        return label
    return None


def _opt_num(step: dict[str, Any], key: str, label: str) -> str | None:
    if key in step and not _is_num(step.get(key)):
        return label
    return None


def _validate_input(step: dict[str, Any]) -> str | None:
    if not _is_int(step.get("index")) or not _is_str(step.get("text")):
        return "input requires integer index and string text"
    mode = step.get("mode", "auto")
    if mode is not None and (not _is_str(mode) or mode not in FILL_MODES):
        return "input mode must be auto, keys, or fast"
    return None


def _validate_press(step: dict[str, Any]) -> str | None:
    if not _is_str(step.get("key")):
        return "press requires string key"
    mods = step.get("modifiers", 0)
    if mods is not None and not _is_int(mods):
        return "press modifiers must be an integer"
    return None


def _validate_click_text(step: dict[str, Any]) -> str | None:
    if not _is_str(step.get("text")):
        return "click_text requires string text"
    role = step.get("role")
    if role is not None and (not _is_str(role) or role not in CLICK_TEXT_ROLES):
        return "click_text role must be button, link, tab, menuitem, checkbox, radio, or option"
    if "exact" in step and not isinstance(step.get("exact"), bool):
        return "click_text exact must be a boolean"
    if "nth" in step and not _is_int(step.get("nth")):
        return "click_text nth must be an integer"
    return None


def _validate_scroll(step: dict[str, Any]) -> str | None:
    if "dy" not in step:
        return "scroll requires dy"
    if not _is_num(step.get("dy")):
        return "scroll dy must be a number"
    return (
        _opt_num(step, "dx", "scroll dx must be a number")
        or _opt_num(step, "x", "scroll x must be a number")
        or _opt_num(step, "y", "scroll y must be a number")
    )


def _validate_open_tab(step: dict[str, Any]) -> str | None:
    if not _is_str(step.get("url")):
        return "open_tab requires string url"
    if "alias" in step and step.get("alias") is not None and not _is_str(step.get("alias")):
        return "open_tab alias must be a string"
    if "focus" in step and not isinstance(step.get("focus"), bool):
        return "open_tab focus must be a boolean"
    return None


def _validate_fill_form(step: dict[str, Any]) -> str | None:
    fields = step.get("fields")
    if not isinstance(fields, dict):
        return "fill_form requires fields object of label to value"
    if not all(_is_str(k) and _is_str(v) for k, v in fields.items()):
        return "fill_form fields must be string label to string value"
    if "submit" in step and not isinstance(step.get("submit"), bool):
        return "fill_form submit must be a boolean"
    mode = step.get("mode", "auto")
    if mode is not None and (not _is_str(mode) or mode not in FILL_MODES):
        return "fill_form mode must be auto, keys, or fast"
    return None


def _validate_login(step: dict[str, Any]) -> str | None:
    origin = step.get("expected_origin")
    if not _is_str(origin) or not origin.strip():
        return "login requires string expected_origin"
    if "username" in step and step.get("username") is not None and not _is_str(step.get("username")):
        return "login username must be a string"
    return None


_STEP_VALIDATORS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "click": lambda s: _req_int(s, "index", "click requires integer index"),
    "input": _validate_input,
    "select": lambda s: (
        "select requires integer index and string text"
        if not _is_int(s.get("index")) or not _is_str(s.get("text"))
        else None
    ),
    "press": _validate_press,
    "click_text": _validate_click_text,
    "wait_for": lambda s: _req_str(s, "selector", "wait_for requires string selector")
    or _opt_num(s, "timeout", "wait_for timeout must be a number"),
    "wait_idle": lambda s: _opt_num(s, "timeout", "wait_idle timeout must be a number"),
    "goto": lambda s: _req_str(s, "url", "goto requires string url"),
    "scroll": _validate_scroll,
    "settle": lambda _s: None,
    "tab": lambda s: _req_str(s, "tab", "tab requires string tab"),
    "open_tab": _validate_open_tab,
    "fill_form": _validate_fill_form,
    "login": _validate_login,
}

_STEP_RUNNERS: dict[str, Callable[[ActContext, dict[str, Any]], dict[str, Any]]] = {
    "click": lambda ctx, s: do_click_by_index(ctx.handle, int(s["index"])),
    "input": lambda ctx, s: do_input_by_index(
        ctx.handle, int(s["index"]), str(s["text"]), mode=str(s.get("mode") or "auto")
    ),
    "select": lambda ctx, s: do_select_by_index(ctx.handle, int(s["index"]), str(s["text"])),
    "press": lambda ctx, s: do_press(
        ctx.handle, str(s["key"]), int(s.get("modifiers") or 0)
    ),
    "click_text": lambda ctx, s: do_click_text(
        ctx.handle,
        str(s["text"]),
        role=str(s["role"]) if s.get("role") else None,
        exact=bool(s.get("exact", False)),
        nth=int(s.get("nth") or 0),
    ),
    "wait_for": lambda ctx, s: do_wait_for(
        ctx.handle, str(s["selector"]), timeout=float(s.get("timeout", 10))
    ),
    "wait_idle": lambda ctx, s: do_wait_idle(ctx.handle, timeout=float(s.get("timeout", 10))),
    "goto": _run_goto,
    "scroll": lambda ctx, s: do_scroll(
        ctx.handle,
        float(s.get("x") or 0),
        float(s.get("y") or 0),
        dy=float(s.get("dy") if s.get("dy") is not None else -300),
        dx=float(s.get("dx") or 0),
    ),
    "settle": lambda ctx, _s: do_settle(ctx.handle),
    "tab": _run_tab,
    "open_tab": _run_open_tab,
    "fill_form": lambda ctx, s: do_fill_form(
        ctx.handle,
        {str(k): str(v) for k, v in (s.get("fields") or {}).items()},
        submit=bool(s.get("submit", False)),
        mode=str(s.get("mode") or "auto"),
    ),
    "login": lambda ctx, s: ctx.login(
        str(s["username"]) if s.get("username") is not None else None,
        str(s["expected_origin"]),
    ),
}
