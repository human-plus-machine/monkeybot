"""Indexed and coordinate actions: click, input, select, fill, keys, scroll, upload."""

from __future__ import annotations

from browser_mcp import actions, backend, dom_indexing, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool
from browser_mcp import results
from browser_mcp.observe import observe_after, resolve_action_observe

@mcp.tool()
@_public_tool
def browser_click_by_index(
    index: int, observe: str | None = None, tab: str | None = None
) -> str:
    """Click an interactive element by index from browser_get_elements. Prefer this over
    browser_click(x, y) — no pixel-guessing, resilient to layout shifts.

    Default observe=\"diff\" returns the settled page snapshot in the response.
    """
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    try:
        payload = actions.do_click_by_index(handle, index)
    except dom_indexing.ElementNotFoundError as exc:
        return results.json_text({"ok": False, "error": str(exc)})
    action: dict[str, Any] = {"type": "click", "index": index, "clicked": payload["clicked"]}
    if payload.get("warning"):
        action["warning"] = payload["warning"]
    wrapped = observe_after(
        handle, mode, action, before_url=before_url, retry_until_change=True
    )
    return results.json_text(results.with_observation(payload, wrapped))

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
    observe_mode = resolve_action_observe(observe)
    if observe_mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(
            observe if observe is not None else observe_mode, results._ACTION_OBSERVE_MODES
        )
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    try:
        payload = actions.do_input_by_index(
            handle, index, text, clear_first=clear_first, mode=mode
        )
    except dom_indexing.ElementNotFoundError as exc:
        return results.json_text({"ok": False, "error": str(exc)})
    wrapped = observe_after(
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
    return results.json_text(results.with_observation(payload, wrapped))

@mcp.tool()
@_public_tool
def browser_select_by_index(
    index: int, text: str, observe: str | None = None, tab: str | None = None
) -> str:
    """Select a <select> dropdown option by visible text, using the index from
    browser_get_elements."""
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    try:
        payload = actions.do_select_by_index(handle, index, text)
    except dom_indexing.ElementNotFoundError as exc:
        return results.json_text({"ok": False, "error": str(exc)})
    wrapped = observe_after(
        handle,
        mode,
        {"type": "select", "index": index, "selected": text},
        before_url=str(handle.page_info().get("url") or ""),
    )
    return results.json_text(results.with_observation(payload, wrapped))

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
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    actions.do_click_xy(handle, x, y, button=button, clicks=clicks)
    wrapped = observe_after(
        handle,
        mode,
        {"type": "click", "x": x, "y": y, "button": button, "clicks": clicks},
        before_url=before_url,
        retry_until_change=True,
    )
    return results.json_text(results.with_observation({"ok": True}, wrapped))

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
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    actions.do_fill_selector(
        handle, selector, text, clear_first=clear_first, timeout=timeout
    )
    wrapped = observe_after(
        handle,
        mode,
        {"type": "fill", "selector": selector},
        before_url=str(handle.page_info().get("url") or ""),
    )
    return results.json_text(results.with_observation({"ok": True}, wrapped))

@mcp.tool()
@_public_tool
def browser_press_key(
    key: str, modifiers: int = 0, observe: str | None = None, tab: str | None = None
) -> str:
    """Press a key. Modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift."""
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    actions.do_press(handle, key, modifiers)
    wrapped = observe_after(
        handle,
        mode,
        {"type": "press", "key": key, "modifiers": modifiers},
        before_url=str(handle.page_info().get("url") or ""),
        retry_until_change=True,
    )
    return results.json_text(results.with_observation({"ok": True}, wrapped))

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
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    actions.do_scroll(handle, x, y, dy=dy, dx=dx)
    wrapped = observe_after(
        handle,
        mode,
        {"type": "scroll", "x": x, "y": y, "dy": dy, "dx": dx},
        before_url=str(handle.page_info().get("url") or ""),
        retry_until_change=True,
    )
    return results.json_text(results.with_observation({"ok": True}, wrapped))

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
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    if role is not None and str(role).strip():
        role_norm = str(role).strip().lower()
        if role_norm not in actions.CLICK_TEXT_ROLES:
            names = ", ".join(sorted(actions.CLICK_TEXT_ROLES))
            return results.json_text({"ok": False, "error": f"unknown role {role!r}; expected {names}"})
    else:
        role_norm = None
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    payload = actions.do_click_text(
        handle, text, role=role_norm, exact=exact, nth=int(nth)
    )
    if not payload.get("ok"):
        return results.json_text(payload)
    wrapped = observe_after(
        handle,
        mode,
        {"type": "click_text", "text": text, "index": payload.get("index")},
        before_url=before_url,
        retry_until_change=True,
    )
    return results.json_text(results.with_observation(payload, wrapped))

@mcp.tool()
@_public_tool
def browser_upload(selector: str, path: str, tab: str | None = None) -> str:
    """Set files on a file input. path must be an absolute filepath on the host."""
    helpers, _ = backend.browser_harness()
    try:
        tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    helpers.upload_file(selector, path)
    return results.json_text({"ok": True})
