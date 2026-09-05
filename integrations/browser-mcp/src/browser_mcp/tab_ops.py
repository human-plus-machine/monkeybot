"""Tab resolution, create/close, and open-tab helpers."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from browser_mcp import actions, dom_indexing, tabs

logger = logging.getLogger(__name__)

_LOAD_WAIT_JS = actions.LOAD_WAIT_JS


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
