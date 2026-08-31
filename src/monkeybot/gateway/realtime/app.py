"""FastAPI application factory for the realtime gateway.

This creates a new FastAPI app that mounts the SSE gateway routes **plus** the realtime
WebSocket endpoint. The existing ``gateway/sse/app.py`` is left untouched.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from monkeybot.core.config.realtime_config import RealtimeConfig, get_realtime_config
from monkeybot.core.config.settings import normalize_model_provider
from monkeybot.core.config.snapshot import current_env
from monkeybot.gateway.sse.routes import create_app as build_sse_app
from monkeybot.providers.gemini_live import GeminiLiveProvider

from .deps import RealtimeDependencies
from .manager import RealtimeSessionManager
from .routes import create_realtime_router

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """SSE startup (turn-based /reply) plus realtime Live provider wiring.

    Chat and talk share this process. SSE ``GatewayLoopPort`` reads the module
    ``gateway_runtime`` singleton from ``gateway.sse.app``, so we must run SSE
    ``_startup`` here — not only the realtime-only lifespan (which left /reply
    with a no-op loop).
    """
    from monkeybot.gateway.sse.app import _shutdown as sse_shutdown
    from monkeybot.gateway.sse.app import _startup as sse_startup
    from monkeybot.gateway.sse.app import gateway_runtime as sse_runtime

    deps: RealtimeDependencies = app.state.realtime_deps
    config: RealtimeConfig = app.state.realtime_config

    await sse_startup(app)

    # Bridge shared process deps into RealtimeDependencies (no second DB open).
    deps.storage = app.state.storage
    deps.mcp = sse_runtime.mcp
    deps.inspectors = list(sse_runtime.inspectors)
    deps.memory = sse_runtime.memory
    deps.hook_manager = sse_runtime.hook_manager
    deps.web_search_tool = sse_runtime.web_search_tool
    deps.run_command_allowed_commands = sse_runtime.run_command_allowed_commands
    deps.run_command_allowed_path_prefixes = sse_runtime.run_command_allowed_path_prefixes
    deps.subagent_registry = sse_runtime.subagent_registry
    deps.attachment_store = getattr(app.state, "attachment_store", None)
    deps.loops_registry = sse_runtime.loops_registry
    deps.computer_tools = sse_runtime.computer_tools
    deps.computer_approvals_persist = sse_runtime.computer_approvals_persist

    realtime_provider = normalize_model_provider(
        config.model.provider or current_env("MODEL_PROVIDER", "google_vertexai")
    )
    if realtime_provider == "google_genai":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("realtime MODEL_PROVIDER=google_genai but GEMINI_API_KEY is not set")
        deps.realtime_provider = GeminiLiveProvider(api_key=api_key)
    else:
        deps.realtime_provider = GeminiLiveProvider()

    deps.freeze()

    try:
        yield
    finally:
        await sse_shutdown(app)


def create_realtime_app(**kwargs: Any) -> FastAPI:
    """Build a FastAPI app serving both SSE routes and the realtime WebSocket endpoint.

    Extra keyword arguments are forwarded to the underlying SSE app factory.
    """
    config = get_realtime_config()
    manager = RealtimeSessionManager(config)
    deps = RealtimeDependencies()

    # Wire the real turn-based loop (same as gateway.sse.app). Without this,
    # build_sse_app installs a no-op default that clears busy and emits nothing —
    # which leaves the chat TUI stuck on "thinking…".
    from monkeybot.gateway.sse.app import GatewayLoopPort, _StaticUsagePortZeros
    from monkeybot.gateway.sse.session_bus import SessionRegistry
    from monkeybot.gateway.sse.workspace_layout import resolve_agent_workspace_root

    registry = kwargs.pop("registry", None) or SessionRegistry(
        workspace_root=resolve_agent_workspace_root()
    )
    loop_port = kwargs.pop("loop_port", None) or GatewayLoopPort(registry)
    usage_port = kwargs.pop("usage_port", None) or _StaticUsagePortZeros()

    app = build_sse_app(
        lifespan=_combined_lifespan,
        registry=registry,
        loop_port=loop_port,
        usage_port=usage_port,
        **kwargs,
    )
    if isinstance(loop_port, GatewayLoopPort):
        loop_port.bind_app(app)

    app.state.realtime_config = config
    app.state.realtime_deps = deps
    app.state.realtime_manager = manager

    if not any(isinstance(m, CORSMiddleware) for m in app.user_middleware):
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    router = create_realtime_router(deps, manager)
    app.include_router(router)

    return app


# Module-level app for ``uvicorn monkeybot.gateway.realtime.app:app``.
app = create_realtime_app()
