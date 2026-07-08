"""FastAPI application factory for the realtime gateway.

This creates a new FastAPI app that mounts the SSE gateway routes **plus** the realtime
WebSocket endpoint. The existing ``gateway/sse/app.py`` is left untouched.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from monkeybot.core.config.realtime_config import RealtimeConfig, get_realtime_config
from monkeybot.core.config.settings import (
    auto_schema_enabled_from_config,
    get_subagent_registry,
    normalize_model_provider,
    vertex_google_search_enabled_from_config,
)
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Provider
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import create_storage_backend
from monkeybot.core.tools.inspector import CommandTierInspector, RulesInspector
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.gateway.bootstrap import ensure_gateway_runtime_env
from monkeybot.gateway.sse.routes import create_app as build_sse_app
from monkeybot.providers.gemini_live import GeminiLiveProvider
from monkeybot.web_search import WebSearchTool, build_backend

from .deps import RealtimeDependencies
from .manager import RealtimeSessionManager
from .routes import create_realtime_router

logger = logging.getLogger(__name__)


def _memory_enabled() -> bool:
    raw = os.environ.get("MONKEYBOT_MEMORY_HOOK_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _memory_storage_uri() -> str:
    uri = os.environ.get("MEMORY_STORAGE_URI", "").strip()
    if uri:
        return uri
    raw_mp = os.environ.get("MEMORY_PATH")
    if raw_mp is not None:
        logger.info(
            "memory: deriving storage URI from MEMORY_PATH; prefer paths.memory_storage_uri in monkeybot.yaml"
        )
    legacy = (raw_mp or "data/memory").strip()
    mem = Path(legacy)
    resolved = mem.resolve() if mem.is_absolute() else (Path.cwd().resolve() / mem).resolve()
    return f"local://{resolved}"


def _tool_denied_patterns() -> list[str]:
    raw = os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS")
    if raw is None:
        return ["rm -rf", "/etc/passwd", "DROP TABLE"]
    return [p.strip() for p in raw.split(",") if p.strip()]


def _resolve_provider() -> Provider:
    from monkeybot.core.config.settings import get_provider_config
    from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider

    mode = normalize_model_provider(os.environ.get("MODEL_PROVIDER", "google_vertexai"))
    if mode == "fake":
        return ScriptedFakeProvider([])
    return get_provider_config(provider=mode).provider


@contextlib.asynccontextmanager
async def _realtime_lifespan(
    app: FastAPI,
    deps: RealtimeDependencies,
    config: RealtimeConfig,
) -> AsyncIterator[None]:
    """Wire storage, MCP, inspectors, memory, and realtime provider."""
    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    backend = create_storage_backend(db_url)
    await backend.open(run_schema=auto_schema_enabled_from_config())
    app.state.storage = backend
    deps.storage = backend

    mcp = MCPClient()
    deps.mcp = mcp
    mcp_config = Path(os.environ.get("MCP_CONFIG", "/app/monkeybot_config/mcp.json"))
    strict = os.environ.get("MCP_STRICT_LOAD", "").strip().lower() in ("1", "true", "yes")
    try:
        await mcp.load_from_config(mcp_config, raise_on_error=strict)
    except OSError as exc:
        logger.info("MCP config skipped (%s): %s", mcp_config, exc)

    tiers_path = Path(
        os.environ.get("COMMAND_ALLOWLIST_CONFIG", "/app/monkeybot_config/command_allowlist.yaml")
    )
    deps.run_command_allowed_commands = None
    deps.run_command_allowed_path_prefixes = None
    inspectors: list[Any] = []
    try:
        tier_insp = CommandTierInspector(tiers_path)
        inspectors.append(tier_insp)
        deps.run_command_allowed_commands = list(tier_insp.allowed_commands)
        deps.run_command_allowed_path_prefixes = list(tier_insp.allowed_path_prefixes)
    except FileNotFoundError:
        logger.info("command tiers missing (%s); allowing all tool calls", tiers_path)
    except Exception as exc:
        logger.exception("command tier load failed: %s", exc)

    denied = _tool_denied_patterns()
    if denied:
        inspectors.append(RulesInspector(denied))
    deps.inspectors = inspectors

    # Realtime model can be a Live-only preview while the main turn-based model
    # stays a regular generateContent model. Use realtime.model.provider if set,
    # otherwise fall back to the main provider.
    realtime_provider = normalize_model_provider(
        config.model.provider or os.environ.get("MODEL_PROVIDER", "google_vertexai")
    )
    if realtime_provider == "google_genai":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("realtime MODEL_PROVIDER=google_genai but GEMINI_API_KEY is not set")
        deps.realtime_provider = GeminiLiveProvider(api_key=api_key)
    else:
        deps.realtime_provider = GeminiLiveProvider()

    provider = _resolve_provider()
    vertex_gs = vertex_google_search_enabled_from_config()
    try:
        backend_ws = build_backend()
        if backend_ws is not None:
            deps.web_search_tool = WebSearchTool(backend_ws)
            logger.info("web search enabled: backend=%s", backend_ws.name)
        else:
            deps.web_search_tool = None
    except Exception as exc:
        logger.warning("web search backend init failed — disabling: %s", exc)
        deps.web_search_tool = None

    if vertex_gs:
        logger.info("vertex google_search grounding enabled")
    if deps.web_search_tool is None and not vertex_gs:
        logger.info("web search disabled (WEB_SEARCH_BACKEND=none)")

    if _memory_enabled():
        try:
            mem_uri = _memory_storage_uri()
            storage = create_workspace_storage(mem_uri)
            model_name = os.environ.get("MODEL_NAME", "gemini-2.5-flash")
            memory = MemorySubsystem(
                storage=storage,
                provider=provider,
                model=model_name,
                memory_uri=mem_uri,
            )
            mgr = HookManager()
            memory.register_hooks(mgr)
            deps.hook_manager = mgr
            deps.memory = memory
            app.state.memory = memory
            logger.info("memory hook enabled (memory_storage_uri=%s)", mem_uri)
        except Exception as exc:
            logger.warning("memory hook init failed — disabling: %s", exc)
    else:
        logger.info("memory hook disabled")

    deps.subagent_registry = get_subagent_registry()

    try:
        yield
    finally:
        if deps.mcp is not None:
            for name in list(getattr(deps.mcp, "_servers", {}).keys()):
                try:
                    await deps.mcp.disconnect(name)
                except Exception:
                    logger.exception("MCP disconnect failed for %s", name)
        if deps.storage is not None:
            await deps.storage.close()


@contextlib.asynccontextmanager
async def _combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
    deps: RealtimeDependencies = app.state.realtime_deps
    config: RealtimeConfig = app.state.realtime_config
    async with _realtime_lifespan(app, deps, config):
        yield


def create_realtime_app(**kwargs: Any) -> FastAPI:
    """Build a FastAPI app serving both SSE routes and the realtime WebSocket endpoint.

    Extra keyword arguments are forwarded to the underlying SSE app factory.
    """
    ensure_gateway_runtime_env()

    config = get_realtime_config()
    manager = RealtimeSessionManager(config)
    deps = RealtimeDependencies()

    # Start from the SSE app factory so existing routes remain available.
    # We override the lifespan with our combined one.
    app = build_sse_app(lifespan=_combined_lifespan, **kwargs)
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
