"""Navigate to a URL."""

from __future__ import annotations

from typing import Any

from browser_mcp import actions, backend, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool, observe_mode
from browser_mcp import results
from browser_mcp.observe import observe_after
from browser_mcp.tools.playbook import _playbook_hints


def _goto_observation(
    handle: tabs.TabHandle, url: str, observe: str, payload: dict[str, Any]
) -> str:
    wrapped = observe_after(
        handle, observe, {"type": "goto", "url": url}, before_url="", retry_until_change=False
    )
    return results.json_text(results.with_observation(payload, wrapped))

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
    mode, error = observe_mode(observe, default="full")
    if error:
        return error
    helpers, _ = backend.browser_harness()
    if new_tab:
        opened = tab_ops._open_tab(helpers, url, alias=None, focus=True)
        if not opened.get("ok"):
            return results.json_text(opened)
        info = {
            "url": opened.get("url"),
            "title": opened.get("title"),
            "tab": opened.get("tab"),
            "alias": opened.get("alias"),
        }
        handle = tab_ops._for_action(helpers, opened.get("tab"))
        return _goto_observation(handle, url, mode, {**info, **_playbook_hints(url)})
    if tab is not None:
        try:
            handle = tab_ops._for_read(helpers, tab)
        except tabs.UnknownTabError as exc:
            return results.unknown_tab_result(exc)
        result = actions.do_goto(handle, url)
        return _goto_observation(handle, url, mode, {**result, **_playbook_hints(url)})
    actions.do_goto(tab_ops._for_action(helpers, None), url)
    handle = tab_ops._for_action(helpers, None)
    info = handle.page_info()
    return _goto_observation(handle, url, mode, {**info, **_playbook_hints(url)})
