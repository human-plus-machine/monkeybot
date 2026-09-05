"""Shared action bodies for single tools and ``browser_act``.

Tool wrappers in ``server.py`` resolve a tab, call these helpers, then attach
an observation. ``browser_act`` runs the same helpers in a loop.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from browser_mcp import dom_indexing
from browser_mcp.tabs import TabHandle

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


def _login_unavailable(
    username: str | None = None, expected_origin: str | None = None
) -> dict[str, Any]:
    return {"ok": False, "loggedIn": False, "error": "login is not available"}


@dataclass
class ActContext:
    """Mutable handle plus tab callbacks so the executor can switch tabs."""

    helpers: Any
    handle: TabHandle
    for_action: Callable[[str | None], TabHandle]
    open_tab: Callable[..., dict[str, Any]]
    login: Callable[..., dict[str, Any]] = _login_unavailable


def _record_success(handle: TabHandle, rec: dict[str, Any]) -> None:
    """Append a sanitized action to the per-host ring. Never raises."""
    url = ""
    if handle.state is not None:
        url = handle.state.url or handle.state.last_url or ""
    if not url:
        return
    try:
        from browser_mcp import playbooks
        from browser_mcp.tabs import registry

        host = playbooks.host_slug(url)
        registry().record_action(host, rec)
    except Exception:
        return


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
    _record_success(handle, {"do": "click", "index": index})
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
    _record_success(handle, {"do": "input", "index": index, "text_len": len(text)})
    return {
        "ok": True,
        "index": index,
        "tagName": result.get("tagName"),
        "mode_used": result.get("mode_used"),
    }


def do_select_by_index(handle: TabHandle, index: int, text: str) -> dict[str, Any]:
    dom_indexing.select_option(handle, index, text)
    _record_success(handle, {"do": "select", "index": index, "text_len": len(text)})
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
    _record_success(handle, {"do": "press", "key": key})
    return {"ok": True, "key": key, "modifiers": modifiers}


def do_scroll(
    handle: TabHandle, x: float, y: float, *, dy: float = -300, dx: float = 0
) -> dict[str, Any]:
    handle.helpers.scroll(x, y, dy=dy, dx=dx)
    _record_success(handle, {"do": "scroll", "dy": dy, "dx": dx})
    return {"ok": True, "x": x, "y": y, "dy": dy, "dx": dx}


def do_wait_for(
    handle: TabHandle, selector: str, *, visible: bool = False, timeout: float = 10.0
) -> dict[str, Any]:
    result = dom_indexing.wait_for_selector(
        handle, selector, visible=visible, timeout=timeout
    )
    found = bool(result.get("found"))
    return {"ok": found, "found": found}


def do_wait_idle(
    handle: TabHandle, *, timeout: float = 10.0, idle_ms: float = 500
) -> dict[str, Any]:
    helpers = handle.helpers
    if not handle.focused:
        settled = dom_indexing.settle(handle)
        return {
            "ok": True,
            "idle": None,
            "quiet": bool(settled.get("quiet", True)),
            "navigated": bool(settled.get("navigated", False)),
            "note": WAIT_IDLE_NOTE,
        }
    wait_idle = getattr(helpers, "wait_for_network_idle", None)
    if not callable(wait_idle):
        settled = dom_indexing.settle(handle)
        return {
            "ok": True,
            "idle": None,
            "quiet": bool(settled.get("quiet", True)),
            "navigated": bool(settled.get("navigated", False)),
            "note": "network idle is not available on this backend; DOM settle was used",
        }
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
    rec: dict[str, Any] = {"do": "click_text", "text": text}
    if role:
        rec["role"] = role
    if found.get("index") is not None:
        rec["index"] = found.get("index")
    _record_success(handle, rec)
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
    _record_success(handle, {"do": "goto"})
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
        resolved = dom_indexing.resolve_field(handle, str(label))
        index = resolved.get("index")
        if index is None:
            unresolved.append(str(label))
            continue
        idx = int(index)
        how = resolved.get("how")
        tag = str(resolved.get("tagName") or "").lower()
        typ = str(resolved.get("type") or "").lower()
        try:
            if tag == "select":
                do_select_by_index(handle, idx, str(value))
            elif tag == "input" and typ in {"checkbox", "radio"}:
                flag = str(value).strip().lower()
                if flag not in {"true", "false"}:
                    unresolved.append(str(label))
                    continue
                dom_indexing.set_checked(handle, idx, flag == "true")
            else:
                do_input_by_index(handle, idx, str(value), mode=mode)
        except (dom_indexing.ElementNotFoundError, RuntimeError):
            unresolved.append(str(label))
            continue
        filled.append({"label": str(label), "index": idx, "how": how})
        last_index = idx
    submitted = False
    if submit and last_index is not None:
        submitted = _submit_form(handle, last_index)
    ok = bool(filled) or not fields
    if fields and not filled:
        ok = False
    if ok:
        _record_success(
            handle,
            {
                "do": "fill_form",
                "labels": [row["label"] for row in filled],
                "submitted": submitted,
            },
        )
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
    if kind == "click":
        if not _is_int(step.get("index")):
            return "click requires integer index"
    elif kind == "input":
        if not _is_int(step.get("index")) or not _is_str(step.get("text")):
            return "input requires integer index and string text"
        mode = step.get("mode", "auto")
        if mode is not None and (not _is_str(mode) or mode not in FILL_MODES):
            return "input mode must be auto, keys, or fast"
    elif kind == "select":
        if not _is_int(step.get("index")) or not _is_str(step.get("text")):
            return "select requires integer index and string text"
    elif kind == "press":
        if not _is_str(step.get("key")):
            return "press requires string key"
        mods = step.get("modifiers", 0)
        if mods is not None and not _is_int(mods):
            return "press modifiers must be an integer"
    elif kind == "click_text":
        if not _is_str(step.get("text")):
            return "click_text requires string text"
        role = step.get("role")
        if role is not None and (not _is_str(role) or role not in CLICK_TEXT_ROLES):
            return "click_text role must be button, link, tab, menuitem, checkbox, radio, or option"
        if "exact" in step and not isinstance(step.get("exact"), bool):
            return "click_text exact must be a boolean"
        if "nth" in step and not _is_int(step.get("nth")):
            return "click_text nth must be an integer"
    elif kind == "wait_for":
        if not _is_str(step.get("selector")):
            return "wait_for requires string selector"
        if "timeout" in step and not _is_num(step.get("timeout")):
            return "wait_for timeout must be a number"
    elif kind == "wait_idle":
        if "timeout" in step and not _is_num(step.get("timeout")):
            return "wait_idle timeout must be a number"
    elif kind == "goto":
        if not _is_str(step.get("url")):
            return "goto requires string url"
    elif kind == "scroll":
        if "dy" not in step:
            return "scroll requires dy"
        if not _is_num(step.get("dy")):
            return "scroll dy must be a number"
        if "dx" in step and not _is_num(step.get("dx")):
            return "scroll dx must be a number"
        if "x" in step and not _is_num(step.get("x")):
            return "scroll x must be a number"
        if "y" in step and not _is_num(step.get("y")):
            return "scroll y must be a number"
    elif kind == "tab":
        if not _is_str(step.get("tab")):
            return "tab requires string tab"
    elif kind == "open_tab":
        if not _is_str(step.get("url")):
            return "open_tab requires string url"
        if "alias" in step and step.get("alias") is not None and not _is_str(step.get("alias")):
            return "open_tab alias must be a string"
        if "focus" in step and not isinstance(step.get("focus"), bool):
            return "open_tab focus must be a boolean"
    elif kind == "fill_form":
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
    elif kind == "login":
        origin = step.get("expected_origin")
        if not _is_str(origin) or not origin.strip():
            return "login requires string expected_origin"
        if "username" in step and step.get("username") is not None and not _is_str(
            step.get("username")
        ):
            return "login username must be a string"
    return None


def execute_steps(
    ctx: ActContext,
    steps: list[dict[str, Any]],
    *,
    stop_on_error: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    failed_step: int | None = None
    error: str | None = None
    extra: dict[str, Any] = {}
    for i, step in enumerate(steps):
        if deadline is not None and time.monotonic() >= deadline:
            return {
                "ok": False,
                "completed": completed,
                "failed_step": i,
                "error": PLAYBOOK_TIMEOUT_ERROR,
                "handle": ctx.handle,
            }
        try:
            result = _run_step(ctx, step)
        except dom_indexing.ElementNotFoundError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        row = {**result, "do": step.get("do"), "step_index": i}
        if not result.get("ok"):
            failed_step = i
            error = str(result.get("error") or "step failed")
            if result.get("did_you_mean") is not None:
                extra["did_you_mean"] = result.get("did_you_mean")
            if stop_on_error:
                out: dict[str, Any] = {
                    "ok": False,
                    "completed": completed,
                    "failed_step": failed_step,
                    "error": error,
                    "handle": ctx.handle,
                }
                out.update(extra)
                return out
            completed.append(row)
            continue
        completed.append(row)
        if step.get("do") not in SKIP_SETTLE:
            dom_indexing.settle(ctx.handle)
    if failed_step is not None:
        out = {
            "ok": False,
            "completed": [row for row in completed if row.get("ok")],
            "failed_step": failed_step,
            "error": error,
            "handle": ctx.handle,
        }
        out.update(extra)
        return out
    return {"ok": True, "steps": completed, "handle": ctx.handle}


def _run_step(ctx: ActContext, step: dict[str, Any]) -> dict[str, Any]:
    kind = str(step["do"])
    handle = ctx.handle
    if kind == "click":
        return do_click_by_index(handle, int(step["index"]))
    if kind == "input":
        return do_input_by_index(
            handle,
            int(step["index"]),
            str(step["text"]),
            mode=str(step.get("mode") or "auto"),
        )
    if kind == "select":
        return do_select_by_index(handle, int(step["index"]), str(step["text"]))
    if kind == "press":
        return do_press(handle, str(step["key"]), int(step.get("modifiers") or 0))
    if kind == "click_text":
        role = step.get("role")
        return do_click_text(
            handle,
            str(step["text"]),
            role=str(role) if role else None,
            exact=bool(step.get("exact", False)),
            nth=int(step.get("nth") or 0),
        )
    if kind == "wait_for":
        timeout = step.get("timeout", 10)
        return do_wait_for(handle, str(step["selector"]), timeout=float(timeout))
    if kind == "wait_idle":
        timeout = step.get("timeout", 10)
        return do_wait_idle(handle, timeout=float(timeout))
    if kind == "goto":
        result = do_goto(handle, str(step["url"]))
        if handle.focused:
            ctx.handle = ctx.for_action(None)
        return result
    if kind == "scroll":
        return do_scroll(
            handle,
            float(step.get("x") or 0),
            float(step.get("y") or 0),
            dy=float(step.get("dy") if step.get("dy") is not None else -300),
            dx=float(step.get("dx") or 0),
        )
    if kind == "settle":
        return do_settle(handle)
    if kind == "tab":
        ctx.handle = ctx.for_action(str(step["tab"]))
        _record_success(ctx.handle, {"do": "tab", "tab": step["tab"]})
        return {"ok": True, "tab": step["tab"]}
    if kind == "open_tab":
        opened = ctx.open_tab(
            str(step["url"]),
            alias=step.get("alias"),
            focus=bool(step.get("focus", False)),
        )
        if not opened.get("ok"):
            return opened
        if opened.get("focused"):
            ctx.handle = ctx.for_action(str(opened.get("tab") or ""))
        _record_success(ctx.handle, {"do": "open_tab"})
        return opened
    if kind == "fill_form":
        fields = step.get("fields") or {}
        return do_fill_form(
            handle,
            {str(k): str(v) for k, v in fields.items()},
            submit=bool(step.get("submit", False)),
            mode=str(step.get("mode") or "auto"),
        )
    if kind == "login":
        username = step.get("username")
        return ctx.login(
            str(username) if username is not None else None,
            str(step["expected_origin"]),
        )
    return {"ok": False, "error": f"unknown do {kind!r}"}
