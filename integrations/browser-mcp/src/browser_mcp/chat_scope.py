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

_unsupported: bool = False


def reset() -> None:
    """Clear the unsupported latch after a daemon (re)bind.

    Does not drop tab registries. Each chat owns its own registry, so a
    rebind must not throw away another chat's aliases.
    """
    global _unsupported
    _unsupported = False
    from browser_mcp import chat_context

    chat_context.clear_announced()


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

    Kept for apps that do not read ``chat=`` on the WebSocket URL. Tabs are
    not dropped here: each chat has its own registry, and the caller decides
    when a connection has already announced (once per daemon bind).
    """
    global _unsupported
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
