"""Production FastAPI application wiring for the SSE gateway (Story 8).

Bootstraps storage backend, MCP, command tiers, and a real :func:`monkeybot.core.runtime.loop.run` driver;
exposes module-level ``app`` for ``uvicorn monkeybot.gateway.sse.app:app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import monkeybot.gateway.load_env  # noqa: F401 — side effect: dotenv + monkeybot.yaml
from monkeybot.core.config.settings import get_provider_config, normalize_model_provider
from monkeybot.core.context import build_context
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import (
    Done,
    Provider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.llm.usage import Usage as UsageRecord
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import (
    StorageBackend,
    UsageStore,
    create_storage_backend,
)
from monkeybot.core.runtime.events import Error as AgentError
from monkeybot.core.runtime.events import TurnComplete, UsageTotals, event_to_json
from monkeybot.core.runtime.loop import SUMMARY_TRIGGER_RATIO
from monkeybot.core.runtime.loop import run as run_loop
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.gateway.sse.loop_port import UsagePort
from monkeybot.gateway.sse.routes import create_app as build_sse_app
from monkeybot.gateway.sse.session_bus import SessionBus, SessionRegistry
from monkeybot.gateway.sse.workspace_layout import resolve_agent_workspace_root
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.web_search import WebSearchTool
from monkeybot.web_search import build_backend as _build_web_search_backend

logger = logging.getLogger(__name__)


@dataclass
class _GatewayDeps:
    """Process-level deps populated on startup (mutable module singleton)."""

    mcp: MCPClient | None = None
    inspectors: list[ToolInspector] = field(default_factory=list)
    provider: Provider | None = None
    curator_provider: Provider | None = None
    hook_manager: HookManager | None = None
    memory: MemorySubsystem | None = None
    web_search_tool: WebSearchTool | None = None
    run_command_allowed_commands: list[str] | None = None
    run_command_allowed_path_prefixes: list[str] | None = None


_deps = _GatewayDeps()


def _memory_enabled() -> bool:
    """Default on; explicit off via ``MONKEYBOT_MEMORY_HOOK_ENABLED=false``."""
    raw = os.environ.get("MONKEYBOT_MEMORY_HOOK_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _env_context_window_tokens() -> int:
    cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "200000").strip()
    try:
        return max(1, int(cap_raw))
    except ValueError:
        return 200_000


def _resolved_workspace_paths() -> tuple[Path, Path]:
    """Resolve agent workspace root and skills dir (relative paths use process ``cwd``)."""
    root = resolve_agent_workspace_root()
    cwd = Path.cwd().resolve()
    skills = Path(os.environ.get("SKILLS_PATH", "skills"))
    skills_p = skills.resolve() if skills.is_absolute() else (cwd / skills).resolve()
    return root, skills_p


def _memory_storage_uri() -> str:
    """Effective memory storage URI (``MEMORY_STORAGE_URI`` or legacy ``MEMORY_PATH``)."""
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


class _UsageStoreAdapter(UsagePort):
    """GET /usage backed by the UsageStore returned from the storage backend."""

    def __init__(self, store: UsageStore) -> None:
        self._store = store

    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, Any]:
        since_ms: int | None = None
        if since is not None and since.isdigit():
            since_ms = int(since)
        s = await self._store.summary(thread_id=session_id, since_ms=since_ms)
        context_window_tokens = _env_context_window_tokens()
        summarization_threshold_tokens = max(1, int(context_window_tokens * SUMMARY_TRIGGER_RATIO))
        return {
            "session_id": session_id,
            "turns": s.turns,
            "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens,
            "cached_tokens": s.cached_tokens,
            "cost_usd": s.cost_usd,
            "period_start": s.period_start_ms if s.period_start_ms is not None else 0,
            "period_end": s.period_end_ms if s.period_end_ms is not None else 0,
            "last_prompt_tokens": s.last_prompt_tokens,
            "estimated_prompt_tokens": s.last_estimated_prompt_tokens,
            "summarization_threshold_tokens": summarization_threshold_tokens,
            "context_window_tokens": context_window_tokens,
        }


class _StaticUsagePortZeros(UsagePort):
    """Pre-startup placeholder so route wiring can construct the FastAPI app."""

    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, Any]:
        del since
        cw = _env_context_window_tokens()
        return {
            "session_id": session_id,
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "period_start": 0,
            "period_end": 0,
            "last_prompt_tokens": 0,
            "estimated_prompt_tokens": 0,
            "summarization_threshold_tokens": max(1, int(cw * SUMMARY_TRIGGER_RATIO)),
            "context_window_tokens": cw,
        }


def _default_agent_path(bus: SessionBus) -> Path:
    if bus.agent_md:
        return Path(bus.agent_md)
    env = os.environ.get("AGENT_MD")
    if not env:
        raise RuntimeError("AGENT_MD must be set when session has no agent_md path")
    return Path(env)


def _scripted_fake_provider() -> Provider:
    """Deterministic provider for ``MODEL_PROVIDER=fake`` (tests and playground)."""
    raw = os.environ.get("MONKEYBOT_FAKE_PROVIDER_EVENTS", "")
    if not raw:
        return ScriptedFakeProvider(
            [
                TextDelta(text="hello"),
                UsageEvent(input_tokens=1, output_tokens=2, cached_tokens=0),
                Done(),
            ]
        )

    decoded = json.loads(raw)
    turns: list[list[ProviderEvent]] = []
    for turn in decoded:
        events: list[ProviderEvent] = []
        if not isinstance(turn, list):
            continue
        for item in turn:
            if not isinstance(item, dict):
                continue
            k = item.get("kind")
            if k == "text_delta":
                events.append(TextDelta(text=str(item.get("text", ""))))
            elif k == "usage":
                events.append(
                    UsageEvent(
                        input_tokens=int(item.get("input_tokens", 0)),
                        output_tokens=int(item.get("output_tokens", 0)),
                        cached_tokens=int(item.get("cached_tokens", 0)),
                    )
                )
            elif k == "tool_call":
                events.append(
                    ToolCall(
                        call_id=str(item["call_id"]),
                        name=str(item["name"]),
                        args=dict(item.get("args", {})),
                    )
                )
            elif k == "done":
                events.append(Done())
        if events:
            turns.append(events)
    if not turns:
        turns = [[TextDelta(text="hello"), Done()]]
    flat: list[ProviderEvent] = [ev for turn in turns for ev in turn]
    return ScriptedFakeProvider(flat)


def _resolve_provider() -> Provider:
    mode = normalize_model_provider(os.environ.get("MODEL_PROVIDER", "google_vertexai"))
    if mode == "fake":
        return _scripted_fake_provider()
    return get_provider_config(provider=mode).provider


def _resolve_curator_provider(main_provider: Provider) -> Provider:
    """Dedicated provider for context curation with thinking and token cap overrides.

    Uses a small ``max_output_tokens`` (the curator only needs ~50 JSON tokens) and
    ``thinking_budget=0`` to explicitly disable extended thinking, which can stall
    preview models for 10s+ on a short JSON-only completion.

    Fake / vertex_anthropic modes reuse ``main_provider`` — curation is no-op in tests
    and vertex-claude has no thinking budget concept.
    """
    mode = normalize_model_provider(os.environ.get("MODEL_PROVIDER", "google_vertexai"))
    if mode in ("fake", "vertex_anthropic"):
        return main_provider
    return GeminiProvider(thinking_budget=0, max_output_tokens=1024)


class GatewayLoopPort:
    """Schedules :func:`~monkeybot.core.runtime.loop.run` and forwards events to the session bus."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    async def start_turn(self, session_id: str, request_id: str, message: str) -> None:
        bus = self._registry.get(session_id)
        if bus is None:
            return

        mcp = _deps.mcp
        inspectors = _deps.inspectors
        provider = _deps.provider

        if mcp is None or provider is None:
            logger.error("gateway deps not initialized")
            await bus.publish_data(
                event_to_json(AgentError(request_id=request_id, error="gateway_not_ready"))
            )
            await bus.publish_data(
                event_to_json(TurnComplete(request_id=request_id, usage=UsageTotals()))
            )
            return

        backend: StorageBackend = app.state.storage
        history = backend.history()
        usage_store = backend.usage()

        cancel_event = asyncio.Event()

        async def _watch_cancel() -> None:
            while True:
                if bus.cancel_requested_for == request_id:
                    cancel_event.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(_watch_cancel())

        executor: CoreToolExecutor | None = None
        try:
            model_name = os.environ.get("MODEL_NAME", "gemini-2.5-flash")
            agent_path = _default_agent_path(bus)

            workspace_root, skills_resolved = _resolved_workspace_paths()

            extra_tools = [_deps.web_search_tool] if _deps.web_search_tool is not None else []

            try:
                ctx = await build_context(
                    session_id,
                    request_id,
                    agent_md_path=agent_path,
                    memory=getattr(app.state, "memory", None),
                    skills_path=skills_resolved,
                    mcp_client=mcp,
                    model=model_name,
                    cancelled=cancel_event,
                    context_window_tokens=_env_context_window_tokens(),
                    workspace_root=workspace_root,
                    sse_bus=bus,
                    extra_tools=extra_tools,
                )
            except Exception as exc:
                logger.exception("build_context failed")
                await bus.publish_data(
                    event_to_json(AgentError(request_id=request_id, error=str(exc)))
                )
                await bus.publish_data(
                    event_to_json(TurnComplete(request_id=request_id, usage=UsageTotals()))
                )
                return

            executor = CoreToolExecutor(
                workspace_root=workspace_root,
                memory=getattr(app.state, "memory", None),
                skills_path=skills_resolved,
                mcp=mcp,
                extra_tools=extra_tools,
                run_command_allowed_commands=_deps.run_command_allowed_commands,
                run_command_allowed_path_prefixes=_deps.run_command_allowed_path_prefixes,
            )
            async for evt in run_loop(
                message,
                ctx,
                provider=provider,
                history=history,
                inspectors=inspectors,
                tool_executor=executor,
                run_id=request_id,
                cancelled=cancel_event,
                hook_manager=_deps.hook_manager,
                curator_provider=_deps.curator_provider,
            ):
                if isinstance(evt, TurnComplete):
                    u = evt.usage
                    await usage_store.record(
                        session_id,
                        model_name,
                        UsageRecord(
                            input_tokens=u.input_tokens,
                            output_tokens=u.output_tokens,
                            cached_tokens=u.cached_tokens,
                            cost_usd=u.cost_usd,
                            duration_ms=u.duration_ms,
                            estimated_prompt_tokens=u.estimated_prompt_tokens,
                        ),
                        run_id=request_id,
                    )
                await bus.publish_data(event_to_json(evt))
        finally:
            if executor is not None:
                await executor.aclose()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            bus.cancel_requested_for = None


def _cors_allow_origins() -> list[str]:
    """Browser origins allowed for cross-origin API calls (SSE, JSON).

    Comma-separated ``MONKEYBOT_CORS_ALLOW_ORIGINS`` overrides the dev default
    (``http://localhost:5173``). Use when the chat UI is hosted on another origin
    and talks to the gateway directly (without a same-origin dev proxy).

    Set to ``*`` to allow any origin (credentials disabled by Starlette rules for ``*``).
    """
    raw = os.environ.get("MONKEYBOT_CORS_ALLOW_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:5173"]
    if raw == "*":
        return ["*"]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts if parts else ["http://localhost:5173"]


_registry = SessionRegistry()
app = build_sse_app(
    registry=_registry,
    loop_port=GatewayLoopPort(_registry),
    usage_port=_StaticUsagePortZeros(),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _tool_denied_patterns() -> list[str]:
    """Substring deny list for :class:`RulesInspector`. Empty ``MONKEYBOT_TOOL_DENIED_PATTERNS`` disables."""
    raw = os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS")
    if raw is None:
        return ["rm -rf", "/etc/passwd", "DROP TABLE"]
    return [p.strip() for p in raw.split(",") if p.strip()]


@app.on_event("startup")
async def _startup() -> None:
    """Wire storage backend, MCP, inspectors, and provider."""
    from monkeybot.observability import init_observability

    otel_enabled = init_observability()
    if otel_enabled:
        logger.info("OpenTelemetry tracing initialized")
    else:
        logger.info("OpenTelemetry tracing not enabled")

    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")

    backend = create_storage_backend(db_url)
    await backend.open()
    app.state.storage = backend
    app.state.usage = _UsageStoreAdapter(backend.usage())

    mcp = MCPClient()
    _deps.mcp = mcp
    mcp_config = Path(os.environ.get("MCP_CONFIG", "/app/monkeybot_config/mcp.json"))
    strict = os.environ.get("MCP_STRICT_LOAD", "").strip().lower() in ("1", "true", "yes")
    try:
        await mcp.load_from_config(mcp_config, raise_on_error=strict)
    except OSError as exc:
        logger.info("MCP config skipped (%s): %s", mcp_config, exc)

    tiers_path = Path(
        os.environ.get("COMMAND_ALLOWLIST_CONFIG", "/app/monkeybot_config/command_allowlist.yaml")
    )
    _deps.run_command_allowed_commands = None
    _deps.run_command_allowed_path_prefixes = None
    inspectors: list[ToolInspector] = []
    try:
        tier_insp = CommandTierInspector(tiers_path)
        inspectors.append(tier_insp)
        _deps.run_command_allowed_commands = list(tier_insp.allowed_commands)
        _deps.run_command_allowed_path_prefixes = list(tier_insp.allowed_path_prefixes)
    except FileNotFoundError:
        logger.info("command tiers missing (%s); allowing all tool calls", tiers_path)
    except Exception as exc:
        logger.exception("command tier load failed: %s", exc)

    denied = _tool_denied_patterns()
    if denied:
        inspectors.append(RulesInspector(denied))
    _deps.inspectors = inspectors

    _deps.provider = _resolve_provider()
    _deps.curator_provider = _resolve_curator_provider(_deps.provider)

    try:
        backend_ws = _build_web_search_backend()
        _deps.web_search_tool = WebSearchTool(backend_ws) if backend_ws is not None else None
        if _deps.web_search_tool is not None:
            logger.info("web search enabled: backend=%s", backend_ws.name)
        else:
            logger.info("web search disabled (WEB_SEARCH_BACKEND=none)")
    except Exception as exc:
        logger.warning("web search backend init failed — disabling: %s", exc)
        _deps.web_search_tool = None

    if _memory_enabled():
        try:
            mem_uri = _memory_storage_uri()
            storage = create_workspace_storage(mem_uri)
            mgr = HookManager()
            model_name = os.environ.get("MODEL_NAME", "gemini-2.5-flash")
            memory = MemorySubsystem(
                storage=storage,
                provider=_deps.provider,
                model=model_name,
                memory_uri=mem_uri,
            )
            memory.register_hooks(mgr)
            _deps.hook_manager = mgr
            _deps.memory = memory
            app.state.memory = memory
            logger.info("memory hook enabled (memory_storage_uri=%s)", mem_uri)
            try:
                gc_stats = await memory.gc_processed()
                if gc_stats["deleted"] or gc_stats["errors"]:
                    logger.info(
                        "memory gc: scanned=%d deleted=%d errors=%d",
                        gc_stats["scanned"],
                        gc_stats["deleted"],
                        gc_stats["errors"],
                    )
            except Exception as gc_exc:
                logger.warning("memory gc on startup failed: %r", gc_exc)
        except Exception as exc:
            logger.warning("memory hook setup failed; continuing without: %r", exc)
            _deps.hook_manager = None
            _deps.memory = None
            app.state.memory = None
    else:
        logger.info("memory hook disabled via MONKEYBOT_MEMORY_HOOK_ENABLED")
        app.state.memory = None

    if otel_enabled:
        from monkeybot.observability.instrumentation import instrument_fastapi_app

        instrument_fastapi_app(app)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Tear down MCP sessions and storage backend."""
    from monkeybot.observability import shutdown_observability

    try:
        shutdown_observability()
    except Exception as exc:
        logger.warning("observability shutdown failed: %s", exc)

    mcp = _deps.mcp
    if mcp is not None:
        for name in list(getattr(mcp, "_servers", {}).keys()):
            await mcp.disconnect(name)

    storage: StorageBackend | None = getattr(app.state, "storage", None)
    if storage is not None:
        await storage.close()


def create_app() -> FastAPI:
    """Return the module-level ASGI app (for symmetry with docs/tests)."""
    return app
