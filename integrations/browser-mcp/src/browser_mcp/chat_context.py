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
_SWEEP_INTERVAL_SECONDS = 30
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
    announce_unsupported: bool = False
    # True after an idle sweep stopped the daemon but kept this context's tabs.
    daemon_released: bool = False
    last_used: float = field(default_factory=time.monotonic)


_shared: ChatContext | None = None
_isolated: dict[str, ChatContext] = {}
_guard = threading.Lock()
# Daemon names whose process is being stopped. ``context_for_call`` waits
# these out so a new chat does not start a daemon that shutdown then kills.
_stopping_daemons: set[str] = set()
_router_installed = False
_sweeper_started = False

# Captured at import, before tests replace ``sys.modules['browser_harness']``.
# ``browser_harness.ipc`` does not exist; the IPC module is ``_ipc``.
_harness_helpers: Any = None
_harness_ipc: Any = None
_harness_import_error: BaseException | None = None
try:
    import browser_harness.helpers as _imported_helpers
    from browser_harness import _ipc as _imported_ipc

    _harness_helpers = _imported_helpers
    _harness_ipc = _imported_ipc
except Exception as exc:  # pragma: no cover - dependency is required
    _harness_import_error = exc


def shared() -> ChatContext:
    """The single context for local Chrome and AgentCore."""
    global _shared
    current = _shared
    if current is not None:
        return current
    with _guard:
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
    context. Idle daemon cleanup runs on a background thread so this lookup
    does not block the caller on ``restart_daemon``.
    """
    _ensure_idle_sweeper()
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
                    ctx.daemon_released = False
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


def clear_unsupported() -> None:
    """The app may have gained ``setChatScope`` after a bridge restart."""
    shared().announce_unsupported = False
    with _guard:
        contexts = list(_isolated.values())
    for ctx in contexts:
        ctx.announce_unsupported = False


def _stop_named_daemon(ctx: ChatContext) -> None:
    """Stop ``ctx``'s daemon without holding ``_guard`` across the blocking call.

    The context stays in ``_isolated`` for idle release and ``drop``, so the
    check below ignores ``ctx`` itself. ``stop_all_isolated`` removes contexts
    first, which is what makes this actually signal the process.
    """
    name = ctx.daemon_name
    if not name:
        return
    with _guard:
        if any(other is not ctx and other.daemon_name == name for other in _isolated.values()):
            return
        _stopping_daemons.add(name)
    try:
        _stop_daemon(ctx)
    finally:
        with _guard:
            _stopping_daemons.discard(name)


def _release_daemon(ctx: ChatContext, *, reset_tabs: bool) -> None:
    """Stop the daemon. Keep the context object so its lock stays the chat's lock.

    Idle release keeps tab aliases: the Chromium tabs are still in Spaces, and
    the next call starts a fresh daemon against the same ``chat=`` URL.
    ``browser_stop`` passes ``reset_tabs=True`` because it already closed them.
    """
    ctx.bh = None
    ctx.bound_cdp = None
    ctx.announced = False
    if reset_tabs:
        ctx.registry.reset()
        ctx.daemon_released = False
    else:
        ctx.daemon_released = True
    _stop_named_daemon(ctx)


def drop(ctx: ChatContext) -> None:
    """Stop one chat's daemon without replacing its context.

    The caller often still holds ``ctx.lock``. Removing the context here would
    make the next call build a new lock and run beside this one.
    """
    if not ctx.isolated:
        return
    _release_daemon(ctx, reset_tabs=True)


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
        _release_daemon(ctx, reset_tabs=True)


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
    stale: list[tuple[ChatContext, float]] = []
    with _guard:
        for ctx in list(_isolated.values()):
            if ctx.daemon_released and ctx.bh is None:
                continue
            if moment - ctx.last_used < IDLE_SECONDS:
                continue
            # RLock has no locked(). A non-blocking acquire means nobody is inside.
            if not ctx.lock.acquire(blocking=False):
                continue
            ctx.lock.release()
            stale.append((ctx, ctx.last_used))
    for ctx, seen_used in stale:
        with _guard:
            # A call landed after the snapshot and refreshed ``last_used``.
            if ctx.last_used != seen_used or moment - ctx.last_used < IDLE_SECONDS:
                continue
            if not ctx.lock.acquire(blocking=False):
                continue
            ctx.lock.release()
        logger.info("browser-mcp: stopping idle chat daemon %s", ctx.daemon_name)
        _release_daemon(ctx, reset_tabs=False)


def _idle_loop() -> None:
    while True:
        time.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            _sweep_idle()
        except Exception:
            logger.warning("browser-mcp: idle sweep failed", exc_info=True)


def _ensure_idle_sweeper() -> None:
    """Start the idle sweep once. It must not run on a tool-call thread."""
    global _sweeper_started
    with _guard:
        if _sweeper_started:
            return
        _sweeper_started = True
    threading.Thread(target=_idle_loop, name="browser-mcp-idle-sweep", daemon=True).start()


def install_send_router() -> None:
    """Send harness IPC through ``browser_harness._ipc`` to this thread's daemon.

    ``helpers._send`` otherwise always connects to ``helpers.NAME``. A missing
    ``browser_harness._ipc`` is a hard failure: swallowing it leaves every chat
    on the default daemon while per-chat daemons sit unused.
    """
    global _router_installed
    if _router_installed:
        return
    if _harness_helpers is None or _harness_ipc is None:
        raise RuntimeError(
            "browser-mcp: per-chat daemon routing requires browser_harness._ipc"
        ) from _harness_import_error
    helpers = _harness_helpers
    ipc = _harness_ipc
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
