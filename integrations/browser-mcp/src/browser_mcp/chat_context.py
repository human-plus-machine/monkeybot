"""Per-chat browser state: harness daemon, tab registry, and lock.

The Spaces in-app browser is one Chromium. Each chat gets its own daemon
(its own CDP WebSocket), tab registry, and lock so two chats never restart
or drive each other's browser. Local Chrome and AgentCore stay on one
shared context.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from browser_mcp.tabs import TabRegistry

logger = logging.getLogger(__name__)

IDLE_SECONDS = 15 * 60
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]+")

current_context: contextvars.ContextVar[ChatContext | None] = contextvars.ContextVar(
    "browser_mcp_chat_context", default=None
)
current_daemon: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "browser_mcp_daemon_name", default=None
)


@dataclass
class ChatContext:
    key: str
    isolated: bool
    daemon_name: str | None
    thread_id: str | None
    registry: TabRegistry = field(default_factory=TabRegistry)
    lock: threading.RLock = field(default_factory=threading.RLock)
    bh: tuple[Any, Any] | None = None
    bound_cdp: str | None = None
    announced: bool = False
    last_used: float = field(default_factory=time.monotonic)


_shared: ChatContext | None = None
_isolated: dict[str, ChatContext] = {}
_guard = threading.Lock()
# Daemon names whose process is being stopped. ``context_for_call`` waits
# these out so a new chat does not start a daemon that shutdown then kills.
_stopping_daemons: set[str] = set()
_router_installed = False


def shared() -> ChatContext:
    """The single context for local Chrome and AgentCore."""
    global _shared
    if _shared is None:
        _shared = ChatContext(
            key="shared", isolated=False, daemon_name=None, thread_id=None
        )
    return _shared


def active() -> ChatContext:
    """Context bound on this thread, else the shared one."""
    return current_context.get() or shared()


def deactivate() -> None:
    """Drop the thread's context binding. Does not stop a daemon."""
    current_context.set(None)
    current_daemon.set(None)


def enter(ctx: ChatContext) -> tuple[contextvars.Token[ChatContext | None], contextvars.Token[str | None]]:
    """Bind ``ctx`` on this thread so harness IPC and the tab registry follow it."""
    ctx.last_used = time.monotonic()
    return current_context.set(ctx), current_daemon.set(ctx.daemon_name)


def leave(
    tokens: tuple[contextvars.Token[ChatContext | None], contextvars.Token[str | None]],
) -> None:
    current_daemon.reset(tokens[1])
    current_context.reset(tokens[0])


def daemon_name_for(run_id: str, thread_id: str) -> str:
    """Daemon name unique to one run+chat. Fits browser-harness's name rule."""
    raw = (os.environ.get("BU_NAME") or "monkeybot").strip() or "monkeybot"
    base = _NAME_SAFE.sub("_", raw)[:40] or "monkeybot"
    digest = hashlib.sha1(f"{run_id}|{thread_id}".encode()).hexdigest()[:16]
    return f"{base}-{digest}"[:64]


def _in_app_isolated() -> bool:
    from browser_mcp import in_app_cdp

    if in_app_cdp._env_set_from_in_app_file:
        return True
    url = in_app_cdp._read_in_app_cdp_file()
    if not url:
        return False
    from urllib.parse import urlparse

    return in_app_cdp._is_loopback_host(urlparse(url).hostname)


def context_for_call(thread_id: str | None) -> ChatContext:
    """Context that should run this tool call.

    In-app chats are isolated by ``(MONKEYBOT_RUN_ID, thread_id)``. A call
    with no thread id, and local Chrome or AgentCore, stay on one shared
    context.
    """
    _sweep_idle()
    tid = (thread_id or "").strip()
    if tid and _in_app_isolated():
        run_id = (os.environ.get("MONKEYBOT_RUN_ID") or "").strip()
        key = f"{run_id}|{tid}"
        name = daemon_name_for(run_id, tid)
        while True:
            with _guard:
                if name not in _stopping_daemons:
                    ctx = _isolated.get(key)
                    if ctx is None:
                        ctx = ChatContext(
                            key=key,
                            isolated=True,
                            daemon_name=name,
                            thread_id=tid or None,
                        )
                        _isolated[key] = ctx
                    ctx.thread_id = tid or None
                    ctx.last_used = time.monotonic()
                    return ctx
            time.sleep(0.05)
    ctx = shared()
    ctx.last_used = time.monotonic()
    return ctx


def active_registry() -> TabRegistry:
    return active().registry


def clear_announced() -> None:
    """A new bridge endpoint needs ``setChatScope`` again on every connection."""
    shared().announced = False
    with _guard:
        contexts = list(_isolated.values())
    for ctx in contexts:
        ctx.announced = False


def _shutdown_context(ctx: ChatContext) -> None:
    """Clear one chat and stop its daemon. Must not be called with ``_guard`` held.

    ``restart_daemon`` can block for many seconds. Holding the creation lock
    across that would stall every other chat's tool call.
    """
    if current_context.get() is ctx:
        deactivate()
    ctx.bh = None
    ctx.bound_cdp = None
    ctx.announced = False
    ctx.registry.reset()
    name = ctx.daemon_name
    if not name:
        return
    with _guard:
        if any(other.daemon_name == name for other in _isolated.values()):
            return
        _stopping_daemons.add(name)
    try:
        _stop_daemon(ctx)
    finally:
        with _guard:
            _stopping_daemons.discard(name)


def drop(ctx: ChatContext) -> None:
    """Forget one chat and stop its daemon. Shared context is not dropped."""
    if not ctx.isolated:
        return
    with _guard:
        _isolated.pop(ctx.key, None)
    _shutdown_context(ctx)


def discard_isolated() -> None:
    """Forget per-chat contexts without stopping daemons. Tests use this."""
    deactivate()
    with _guard:
        _isolated.clear()


def stop_all_isolated() -> None:
    """Stop every per-chat daemon. Used on process exit."""
    with _guard:
        contexts = list(_isolated.values())
        _isolated.clear()
    for ctx in contexts:
        _shutdown_context(ctx)


def _stop_daemon(ctx: ChatContext) -> None:
    if not ctx.daemon_name:
        return
    try:
        from browser_harness import admin

        admin.restart_daemon(ctx.daemon_name)
    except Exception:
        logger.warning(
            "browser-mcp: failed to stop daemon %s", ctx.daemon_name, exc_info=True
        )


def _sweep_idle(now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    stale: list[ChatContext] = []
    with _guard:
        for ctx in list(_isolated.values()):
            if moment - ctx.last_used < IDLE_SECONDS:
                continue
            # RLock has no locked(). A non-blocking acquire means nobody is inside.
            if not ctx.lock.acquire(blocking=False):
                continue
            ctx.lock.release()
            _isolated.pop(ctx.key, None)
            stale.append(ctx)
    for ctx in stale:
        logger.info("browser-mcp: stopping idle chat daemon %s", ctx.daemon_name)
        _shutdown_context(ctx)


def install_send_router() -> None:
    """Send harness IPC to the daemon bound on this thread, not the global name."""
    global _router_installed
    if _router_installed:
        return
    try:
        import browser_harness.helpers as helpers
        import browser_harness.ipc as ipc
    except Exception:
        logger.debug("browser-harness not importable; daemon routing not installed", exc_info=True)
        return
    if getattr(helpers, "_monkeybot_send_routed", False):
        _router_installed = True
        return

    def _send(req: object) -> dict[str, Any]:
        name = current_daemon.get() or helpers.NAME
        connection, token = ipc.connect(name, timeout=5.0)
        try:
            response = ipc.request(connection, token, req)
        finally:
            connection.close()
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    helpers._send = _send
    helpers._monkeybot_send_routed = True
    _router_installed = True
