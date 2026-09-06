"""Tab list, switch, open, close, and fan-out read."""

from __future__ import annotations

import logging
from typing import Any

from browser_mcp import backend, dom_indexing, in_app_cdp, login, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool, observe_mode
from browser_mcp import results
from browser_mcp.observe import observe_after

logger = logging.getLogger(__name__)


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
    result = login._sealed_login(username, expected_origin)
    return results.json_text(result)


@mcp.tool()
@_public_tool
def browser_passkey(expected_origin: str | None = None) -> str:
    """Sign in with a saved Spaces passkey without revealing any key material.

    Call this only when the user asked to sign in and a passkey (not a saved
    password) is what's stored for the site. Returns {ok, loggedIn, origin,
    mode: "passkey"} — never any credential material.

    UNVERIFIED (phase 4.4): this has not been exercised against a live
    browser or a real passkey-enabled site — see docs/credential-broker.md
    in the Spaces repo. Prefer browser_login when a saved password exists.

    Signs in to the tab the user has focused, same caveat as browser_login:
    pass expected_origin to make the bridge refuse a mismatched site, and
    always check the returned origin before reporting success.
    """
    result = login._sealed_passkey(expected_origin)
    return results.json_text(result)


@mcp.tool()
@_public_tool
def browser_stop() -> str:
    """Stop the active browser backend (cleanup after browsing; important for cloud/AgentCore browsers)."""
    try:
        backend.stop_active_backend_best_effort()
    except Exception as exc:
        backend.mark_unbound()
        message = in_app_cdp._redact_cdp_token(str(exc))
        logger.warning("browser_stop: failed to stop browser backend: %s", message)
        return results.json_text({"ok": False, "error": message})
    backend.mark_unbound()
    return results.json_text({"ok": True, "message": "browser backend stopped"})
