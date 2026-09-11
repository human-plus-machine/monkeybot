"""Chat identity for the Spaces in-app CDP bridge.

The gateway may pass ``_meta.monkeybot.thread_id`` on each MCP tool call.
When driving the in-app browser, we announce that id as ``Monkeybot.setChatScope``
before other CDP commands so new tabs attach to the right chat.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_UNSET: object = object()
_last_sent: object = _UNSET
_unsupported: bool = False


def reset() -> None:
    """Clear last-sent / unsupported flags after a daemon (re)bind."""
    global _last_sent, _unsupported
    _last_sent = _UNSET
    _unsupported = False


def invalidate_last_sent() -> None:
    """Forget the last announced thread id without clearing the unsupported latch."""
    global _last_sent
    _last_sent = _UNSET


def current_thread_id() -> str | None:
    """Return the gateway thread id from this request's ``_meta``, if any.

    Never raises. Returns ``None`` outside a request or when the extra is absent.
    """
    try:
        from browser_mcp.app import mcp

        ctx = mcp.get_context()
        meta = getattr(getattr(ctx, "request_context", None), "meta", None)
        return _thread_id_from_meta(meta)
    except LookupError:
        return None
    except Exception:
        logger.debug("browser-mcp: current_thread_id failed", exc_info=True)
        return None


def _thread_id_from_meta(meta: object) -> str | None:
    if meta is None:
        return None
    monkeybot: Any = getattr(meta, "monkeybot", None)
    if monkeybot is None:
        extra = getattr(meta, "model_extra", None)
        if isinstance(extra, dict):
            monkeybot = extra.get("monkeybot")
        elif isinstance(meta, dict):
            monkeybot = meta.get("monkeybot")
    if isinstance(monkeybot, dict):
        thread_id = monkeybot.get("thread_id")
    else:
        thread_id = getattr(monkeybot, "thread_id", None)
    if thread_id is None:
        return None
    text = str(thread_id).strip()
    return text or None


def _forget_other_chat_tabs() -> None:
    """Drop the tab registry: its targets belong to the chat we just left.

    Without this, the focused-tab path reuses the previous chat's target and the
    agent drives a page the user cannot see from the current chat.
    """
    from browser_mcp import tabs

    tabs.reset_registry()


def _looks_like_unknown_method(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "unknown method" in text or "method not found" in text


def announce_current(helpers: object) -> None:
    """Announce this request's thread id. Never raises."""
    announce(helpers, current_thread_id())


def announce(helpers: object, thread_id: str | None) -> None:
    """Send ``Monkeybot.setChatScope`` for this request. Never raises.

    CDP is sent on every call (idempotent) so a reconnected WebSocket still gets
    ``chatKey``. The tab registry is dropped only when the thread id changes.
    ``_last_sent`` is updated even when the app does not support the method, so a
    chat switch still drops tabs owned by the previous chat.
    """
    global _last_sent, _unsupported
    previous = _last_sent
    switched = previous is not _UNSET and previous != thread_id and thread_id is not None
    _last_sent = thread_id
    if switched:
        _forget_other_chat_tabs()
        logger.info(
            "browser-mcp: chat scope changed %s -> %s; dropping tabs",
            previous,
            thread_id,
        )
    if _unsupported:
        return
    cdp = getattr(helpers, "cdp", None)
    if not callable(cdp):
        _unsupported = True
        logger.warning("browser-mcp: helpers have no cdp(); disabling setChatScope")
        return
    try:
        cdp("Monkeybot.setChatScope", chatKey=thread_id)
    except Exception as exc:
        if _looks_like_unknown_method(exc):
            _unsupported = True
            logger.warning(
                "browser-mcp: Monkeybot.setChatScope unsupported; disabling for this connection"
            )
        else:
            logger.warning("browser-mcp: Monkeybot.setChatScope failed", exc_info=True)
