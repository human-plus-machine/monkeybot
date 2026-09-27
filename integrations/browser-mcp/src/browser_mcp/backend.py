"""Browser backend binding: in-app CDP, local harness daemon, or AgentCore.

In-app chats each own a :class:`browser_mcp.chat_context.ChatContext` (daemon,
tab registry, lock). Local Chrome and AgentCore use
:func:`browser_mcp.chat_context.shared`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from browser_mcp import agentcore, chat_context, chat_scope, dom_indexing, in_app_cdp, perf

logger = logging.getLogger(__name__)

_agentcore_admin: agentcore.AgentCoreAdmin | None = None


def mark_unbound() -> None:
    chat_context.active().bound_cdp = None


def in_app_backend_active() -> bool:
    """True when the live binding is Monkeyapp's in-app CDP bridge."""
    ctx = chat_context.active()
    return (
        ctx.bh is not None
        and ctx.bound_cdp not in (None, "agentcore")
        and in_app_cdp._env_set_from_in_app_file
    )


def in_app_helpers() -> Any | None:
    """Return in-app CDP helpers when the in-app backend is bound, else None."""
    if not in_app_backend_active():
        return None
    ctx = chat_context.active()
    assert ctx.bh is not None
    helpers, _ = ctx.bh
    return helpers


def _ensure_active_context() -> chat_context.ChatContext:
    """Bind the context for this call when the tool wrapper has not already."""
    ctx = chat_context.context_for_call(chat_scope.current_thread_id())
    if chat_context.current_context.get() is not ctx:
        chat_context.current_context.set(ctx)
        chat_context.current_daemon.set(ctx.daemon_name)
    return ctx


def teardown_bound_backend() -> None:
    """Tear down whatever backend the active context is bound to, if any.

    Clears ``bh`` before attempting the (possibly failing) teardown call, so a
    raised exception here never leaves stale backend state behind for the next
    ``browser_harness()`` call to mistakenly reuse. Dispatches on ``bound_cdp``
    rather than introspecting the admin object, since the non-agentcore path
    always re-imports (and stops) the real ``browser_harness.admin`` module.
    """
    ctx = chat_context.active()
    if ctx.bh is None:
        return
    helpers, admin = ctx.bh
    is_agentcore = ctx.bound_cdp == "agentcore"
    with contextlib.suppress(Exception):
        ctx.registry.detach_all(helpers)
    ctx.bh = None
    ctx.bound_cdp = None
    dom_indexing.clear_registered_targets()
    chat_scope.reset()
    if is_agentcore:
        admin.stop_session()
        from browser_mcp import playwright_helpers

        playwright_helpers.disconnect()
        return
    if ctx.isolated:
        chat_context.drop(ctx)
        return
    from browser_harness import admin as bh_admin

    bh_admin.restart_daemon()


def _with_perf_helpers(bh: tuple[Any, Any]) -> tuple[Any, Any]:
    helpers, admin = bh
    return perf.wrap_helpers(helpers), admin


def _announce_in_app(helpers: Any, ctx: chat_context.ChatContext) -> None:
    """Tell an older app which chat this connection serves. Once per bind.

    Current apps read ``chat=`` from the WebSocket URL, so a repeat announce
    is only needed after the daemon reconnects. Playbook/login skip this by
    not binding.
    """
    if not in_app_cdp._env_set_from_in_app_file or ctx.announced:
        return
    chat_scope.announce(helpers, ctx.thread_id)
    ctx.announced = True


def _reconnect_agentcore() -> tuple[str, dict[str, str]]:
    """Force a fresh AgentCore session (stop + restart) and return new ws creds.

    Registered with playwright_helpers as its reconnect hook: a stale/expired
    AgentCore session (~15-30 min TTL) leaves the old ws connection dead, and
    plain ensure_session() would just re-sign headers for the same (already
    dead) session, so the old session is explicitly stopped first.
    """
    assert _agentcore_admin is not None
    _agentcore_admin.stop_session()
    return _agentcore_admin.ensure_session()


def _agentcore_browser_harness(ctx: chat_context.ChatContext) -> tuple[Any, Any]:
    """Bind the shared context to AgentCore (StartBrowserSession + Playwright)."""
    global _agentcore_admin
    from browser_mcp import playwright_helpers

    if _agentcore_admin is None:
        _agentcore_admin = agentcore.AgentCoreAdmin(agentcore.resolve_region())

    ws_url, headers = _agentcore_admin.ensure_session()
    playwright_helpers.connect(ws_url, headers)
    playwright_helpers.set_reconnect_hook(_reconnect_agentcore)
    ctx.bh = (playwright_helpers, _agentcore_admin)
    ctx.bound_cdp = "agentcore"
    return ctx.bh


def _ensure_harness_daemon(
    ctx: chat_context.ChatContext, endpoint: str | None
) -> tuple[Any, Any]:
    """Start or reuse this context's daemon without touching any other chat's."""
    from browser_harness import admin, helpers

    if ctx.bh is not None and endpoint == ctx.bound_cdp:
        return ctx.bh

    name = ctx.daemon_name
    alive = admin.daemon_alive(name) if name else admin.daemon_alive()
    if alive and ctx.bound_cdp != endpoint:
        logger.info(
            "browser-mcp: replacing harness daemon %s for CDP %s (was %s)",
            name or "default",
            in_app_cdp._redact_cdp_token(str(endpoint)) if endpoint else endpoint,
            in_app_cdp._redact_cdp_token(str(ctx.bound_cdp)) if ctx.bound_cdp else ctx.bound_cdp,
        )
        if name:
            admin.restart_daemon(name)
        else:
            admin.restart_daemon()
        ctx.bh = None
        ctx.announced = False

    env: dict[str, str] | None = None
    if ctx.isolated and endpoint:
        env = {"BU_CDP_WS": endpoint} if endpoint.startswith(("ws://", "wss://")) else {"BU_CDP_URL": endpoint}
    if name:
        admin.ensure_daemon(name=name, env=env)
    else:
        admin.ensure_daemon()
    ctx.bh = (helpers, admin)
    ctx.bound_cdp = endpoint
    return ctx.bh


def browser_harness() -> tuple[Any, Any]:
    """Lazy import + daemon bootstrap on first browser tool use.

    When an explicit CDP URL is configured (env or Monkeyapp runtime file) and the
    live daemon was bound to a different endpoint (or none — i.e. local Chrome),
    bounce that daemon so tool calls drive the in-app panel instead.
    BROWSER_BACKEND=agentcore (with no explicit CDP endpoint) dispatches to AWS
    Bedrock AgentCore Browser instead.

    In-app chats each get their own daemon. Bouncing one never restarts another.
    """
    chat_context.install_send_router()
    in_app_cdp._apply_in_app_cdp_url()
    ctx = _ensure_active_context()
    endpoint = (
        in_app_cdp.daemon_cdp_endpoint(ctx.thread_id) if ctx.isolated else in_app_cdp._apply_in_app_cdp_url()
    )

    if agentcore.agentcore_backend_requested():
        chat_context.stop_all_isolated()
        shared = chat_context.shared()
        if chat_context.current_context.get() is not shared:
            chat_context.current_context.set(shared)
            chat_context.current_daemon.set(None)
        if shared.bh is not None and shared.bound_cdp == "agentcore":
            return _with_perf_helpers(shared.bh)
        teardown_bound_backend()
        return _with_perf_helpers(_agentcore_browser_harness(shared))

    if ctx.bound_cdp == "agentcore":
        teardown_bound_backend()
        ctx = _ensure_active_context()

    if ctx.bh is not None and endpoint == ctx.bound_cdp:
        wrapped = _with_perf_helpers(ctx.bh)
        _announce_in_app(wrapped[0], ctx)
        return wrapped

    bh = _ensure_harness_daemon(ctx, endpoint)
    _announce_in_app(bh[0], ctx)
    return _with_perf_helpers(bh)


def stop_active_backend_best_effort() -> None:
    """Stop the backend for the active chat.

    ``browser_stop`` uses this so one chat does not tear down another chat's
    daemon. Process exit uses :func:`stop_all_backends_best_effort`, which
    also stops a leftover default daemon this process may never have bound.
    """
    from browser_mcp import tab_ops

    ctx = chat_context.active()
    if ctx.bound_cdp == "agentcore":
        if ctx.bh is not None:
            helpers, _ = ctx.bh
            with contextlib.suppress(Exception):
                tab_ops._close_agent_opened_tabs(helpers)
        teardown_bound_backend()
        return
    if ctx.bh is not None:
        helpers, _ = ctx.bh
        with contextlib.suppress(Exception):
            tab_ops._close_agent_opened_tabs(helpers)
    ctx.bh = None
    ctx.bound_cdp = None
    ctx.announced = False
    if ctx.isolated:
        chat_context.drop(ctx)
        return
    chat_scope.reset()
    from browser_harness import admin

    admin.restart_daemon()


def stop_all_backends_best_effort() -> None:
    """Stop every per-chat daemon, then the shared/default one.

    Shutdown must still restart the default daemon even when this process
    never bound it: a leftover Browser Use Cloud daemon from an earlier
    process keeps billing until something stops it.
    """
    chat_context.stop_all_isolated()
    chat_context.deactivate()
    stop_active_backend_best_effort()
