"""Tab list, switch, open, close, and fan-out read."""

from __future__ import annotations

from typing import Any

from browser_mcp import backend, dom_indexing, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool, observe_mode
from browser_mcp import results
from browser_mcp.observe import observe_after


@mcp.tool()
@_public_tool
def browser_tabs() -> str:
    """List open browser tabs with aliases, focus, and last-used times."""
    helpers, _ = backend.browser_harness()
    reg = tabs.registry()
    reg.refresh(helpers)
    return results.json_text(reg.list_payload())

@mcp.tool()
@_public_tool
def browser_switch_tab(target_id: str, observe: str | None = None) -> str:
    """Switch to a tab by alias or target_id (from browser_tabs)."""
    mode, error = observe_mode(observe)
    if error:
        return error
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, target_id)
    except tabs.UnknownTabError:
        sid = helpers.switch_tab(target_id)
        dom_indexing.mark_driver_stale()
        dom_indexing._register_driver_for_new_documents(helpers)
        handle = tab_ops._for_action(helpers, None)
        payload = {"ok": True, "session_id": sid}
        wrapped = observe_after(
            handle,
            mode,
            {"type": "switch_tab", "tab": target_id},
            before_url=str(handle.page_info().get("url") or ""),
            retry_until_change=True,
        )
        return results.json_text(results.with_observation(payload, wrapped))
    sid = handle.switch_session_id
    if sid is None and handle.state is not None:
        sid = helpers.switch_tab(handle.state.target_id)
    dom_indexing._register_driver_for_new_documents(handle)
    payload = {"ok": True, "session_id": sid}
    wrapped = observe_after(
        handle,
        mode,
        {"type": "switch_tab", "tab": target_id},
        before_url=str(handle.page_info().get("url") or ""),
        retry_until_change=True,
    )
    return results.json_text(results.with_observation(payload, wrapped))

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
    helpers, _ = backend.browser_harness()
    try:
        opened = tab_ops._open_tab(helpers, url, alias=alias, focus=focus)
    except (ValueError, tabs.UnknownTabError) as exc:
        return results.json_text({"ok": False, "error": str(exc)})
    if not opened.get("ok") or not focus:
        return results.json_text(opened)
    mode, error = observe_mode(observe, default="full")
    if error:
        return error
    handle = tab_ops._for_action(helpers, str(opened.get("tab") or ""))
    wrapped = observe_after(
        handle,
        mode,
        {"type": "open_tab", "url": url, "tab": opened.get("tab")},
        before_url="",
    )
    return results.json_text(results.with_observation(opened, wrapped))

@mcp.tool()
@_public_tool
def browser_close_tab(tab: str) -> str:
    """Close a tab by alias or target id.

    Refuses to close the last tab (navigates it to about:blank instead). If the
    closed tab was focused, focuses the most recently used remaining tab.
    """
    helpers, _ = backend.browser_harness()
    reg = tabs.registry()
    try:
        reg.refresh(helpers)
        state = reg.resolve(tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    was_focused = state.target_id == reg.focused_id
    remaining = [s for s in reg.tabs() if s.target_id != state.target_id]
    if not remaining:
        handle = tab_ops._for_action(helpers, state.tab)
        handle.navigate("about:blank")
        state.url = "about:blank"
        state.title = ""
        return results.json_text(
            {
                "ok": True,
                "closed": False,
                "blanked": state.tab,
                "focused": state.tab,
                "note": "last tab was navigated to about:blank instead of closed",
            }
        )
    tab_ops._close_target(helpers, state.target_id)
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
    return results.json_text({"ok": True, "closed": closed, "focused": focused_tab})

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
    helpers, _ = backend.browser_harness()
    from browser_mcp.tabs import UnknownTabError, registry as get_registry

    reg = get_registry()
    reg.refresh(helpers)
    if selected:
        try:
            states = [reg.resolve(name) for name in selected]
        except UnknownTabError as exc:
            return results.unknown_tab_result(exc)
    else:
        states = reg.agent_opened()
    out: list[dict[str, Any]] = []
    mode_norm = (mode or "text").strip().lower()
    cap = max(1, int(max_chars))
    for state in states:
        handle = tab_ops._for_read(helpers, state.tab)
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
    return results.json_text({"ok": True, "tabs": out})
