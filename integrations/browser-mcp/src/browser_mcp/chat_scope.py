"""Chat identity for the Spaces in-app CDP bridge.

The gateway may pass ``_meta.monkeybot.thread_id`` on each MCP tool call.
When driving the in-app browser, we announce that id as ``Monkeybot.setChatScope``
before other CDP commands so new tabs attach to the right chat.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# JSON-RPC "Method not found" — Chrome's reply when the app has no setChatScope.
_JSONRPC_METHOD_NOT_FOUND = -32601
_CDP_ERROR_CODE_RE = re.compile(r"""['"]code['"]\s*:\s*(-?\d+)""")

_UNSET: object = object()
_last_chat: object = _UNSET
_unsupported: bool = False


def reset() -> None:
    """Clear last-chat / unsupported flags after a daemon (re)bind."""
    global _last_chat, _unsupported
    _last_chat = _UNSET
    _unsupported = False


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
    """Read ``monkeybot.thread_id`` from FastMCP's pydantic ``RequestParams.Meta``."""
    if meta is None:
        return None
    monkeybot: Any = getattr(meta, "monkeybot", None)
    if not isinstance(monkeybot, dict):
        return None
    thread_id = monkeybot.get("thread_id")
    if thread_id is None:
        return None
    text = str(thread_id).strip()
    return text or None


def _forget_other_chat_tabs() -> None:
    """Drop this process's tab registry: those targets belong to the chat we left.

    Spaces owns the previous chat's tabs and keeps them in that chat's panel.
    Forgetting ``opened_by_agent`` here is intentional: ``browser_stop`` in the
    new chat must not close the previous chat's tabs.
    """
    from browser_mcp import tabs

    tabs.reset_registry()


def _cdp_error_code(exc: BaseException) -> int | None:
    """Best-effort CDP/JSON-RPC error code from a harness ``RuntimeError``."""
    payload = exc.args[0] if exc.args else None
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int):
            return code
    match = _CDP_ERROR_CODE_RE.search(str(exc))
    if match:
        return int(match.group(1))
    return None


def _looks_like_unknown_method(exc: BaseException) -> bool:
    if _cdp_error_code(exc) == _JSONRPC_METHOD_NOT_FOUND:
        return True
    text = str(exc).lower()
    return "unknown method" in text or "method not found" in text


def announce_current(helpers: object) -> None:
    """Announce this request's thread id. Never raises."""
    announce(helpers, current_thread_id())


def announce(helpers: object, thread_id: str | None) -> None:
    """Send ``Monkeybot.setChatScope`` for this request. Never raises.

    CDP is sent on every call (idempotent) so a reconnected WebSocket still gets
    ``chatKey``. The tab registry is dropped only when the non-None thread id
    changes. ``_last_chat`` is updated even when the app does not support the
    method, so a chat switch still drops tabs owned by the previous chat.
    """
    global _last_chat, _unsupported
    previous = _last_chat
    switched = (
        thread_id is not None and previous is not _UNSET and previous != thread_id
    )
    if thread_id is not None:
        _last_chat = thread_id
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
