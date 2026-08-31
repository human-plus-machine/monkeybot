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
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from monkeybot.computer import build_computer_tools, should_enable_computer_tools
from monkeybot.computer.permissions import build_computer_permission_inspector, build_persist_hook
from monkeybot.core.attachments.config import attachments_enabled_from_env
from monkeybot.core.attachments.store import AttachmentStore, FilesystemAttachmentStore
from monkeybot.core.config.settings import (
    ConfigError,
    SubagentConfig,
    auto_schema_enabled_from_config,
    get_provider_config,
    get_subagent_registry,
    normalize_model_provider,
    transcript_enabled_from_config,
    vertex_google_search_enabled_from_config,
)
from monkeybot.core.context import LoopsToolRegistry, build_context
from monkeybot.core.hooks import HookManager
from monkeybot.core.knowledge import KnowledgeSubsystem, resolve_knowledge_settings
from monkeybot.core.knowledge.config import (
    knowledge_enabled_from_config,
    knowledge_read_only_from_env,
)
from monkeybot.core.layout import AgentLayout
from monkeybot.core.llm.provider import (
    Done,
    Provider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.llm.usage import Usage as UsageRecord
from monkeybot.core.llm.usage import UsageGranularity
from monkeybot.core.logging_utils import kv
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.config import memory_enabled_from_config
from monkeybot.core.memory.subsystem import MemoryConfigurationError, MemorySubsystem
from monkeybot.core.persistence.backends import (
    StorageBackend,
    UsageStore,
    create_storage_backend,
)
from monkeybot.core.persistence.transcript import (
    TranscriptWriter,
    runtime_manifest_fields,
)
from monkeybot.core.runtime.events import AgentEvent, TurnComplete, UsageTotals, event_to_json
from monkeybot.core.runtime.events import Error as AgentError
from monkeybot.core.runtime.loop import SUMMARY_TRIGGER_RATIO
from monkeybot.core.runtime.loop import run as run_loop
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.tools.loop_inspector import LoopStartInspector
from monkeybot.core.tools.permission import try_load_permission_inspector
from monkeybot.core.types.content_blocks import ContentBlock, Text
from monkeybot.gateway.bootstrap import ensure_gateway_runtime_env, log_gateway_startup
from monkeybot.gateway.sse.loop_port import UsagePort
from monkeybot.gateway.sse.models import AgentUsageResponse, SessionUsageResponse
from monkeybot.gateway.sse.routes import create_app as build_sse_app
from monkeybot.gateway.sse.session_bus import SessionBus, SessionRegistry
from monkeybot.todo_list import TodoListStore, TodoListTool, todo_list_enabled_from_env
from monkeybot.web_search import WebSearchTool
from monkeybot.web_search import build_backend as _build_web_search_backend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SessionBusEventPublisher:
    """Adapt :class:`SessionBus` to :class:`EventPublisherPort` for nested subagent SSE."""

    bus: SessionBus

    async def publish_event(self, event: AgentEvent) -> None:
        # Nested lane keeps chatty Subagent* traffic from evicting primary-turn replay.
        await self.bus.publish_data(event_to_json(event), lane="nested")


@dataclass
class _GatewayDeps:
    """Process-level deps populated on startup (mutable module singleton)."""

    mcp: MCPClient | None = None
    inspectors: list[ToolInspector] = field(default_factory=list)
    provider: Provider | None = None
    hook_manager: HookManager | None = None
    memory: MemorySubsystem | None = None
    knowledge: KnowledgeSubsystem | None = None
    web_search_tool: WebSearchTool | None = None
    run_command_allowed_commands: list[str] | None = None
    run_command_allowed_path_prefixes: list[str] | None = None
    subagent_registry: dict[str, SubagentConfig] = field(default_factory=dict)
    loops_registry: LoopsToolRegistry = field(default_factory=LoopsToolRegistry)
    computer_tools: list[Any] = field(default_factory=list)
    computer_approvals_persist: Callable[[str, str], bool] | None = None


_deps = _GatewayDeps()


def _env_context_window_tokens() -> int:
    cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "200000").strip()
    try:
        return max(1, int(cap_raw))
    except ValueError:
        return 200_000


def _resolved_workspace_paths() -> tuple[Path, Path, Path | None]:
    """Resolve writable workspace, read-only skills, and artifacts mount from the agent layout."""
    layout = AgentLayout.from_environment()
    return layout.workspace_root, layout.skills_path, layout.artifacts_path


def _memory_storage_uri() -> str:
    """Effective memory storage URI (``MEMORY_STORAGE_URI`` or legacy ``MEMORY_PATH``)."""
    return AgentLayout.from_environment().memory_storage_uri


class _UsageStoreAdapter(UsagePort):
    """Usage endpoints backed by the UsageStore from the storage backend."""

    def __init__(self, store: UsageStore) -> None:
        self._store = store

    @staticmethod
    def _parse_since(since: str | None) -> int | None:
        if since is not None and since.isdigit():
            return int(since)
        return None

    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, Any]:
        since_ms = self._parse_since(since)
        s = await self._store.summary(thread_id=session_id, since_ms=since_ms)
        context_window_tokens = _env_context_window_tokens()
        summarization_threshold_tokens = max(1, int(context_window_tokens * SUMMARY_TRIGGER_RATIO))
        return {
            "session_id": session_id,
            "turns": s.turns,
            "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens,
            "cached_tokens": s.cached_tokens,
            "cache_read_tokens": s.cache_read_tokens,
            "cache_creation_tokens": s.cache_creation_tokens,
            "cost_usd": s.cost_usd,
            "period_start": s.period_start_ms if s.period_start_ms is not None else 0,
            "period_end": s.period_end_ms if s.period_end_ms is not None else 0,
            "last_prompt_tokens": s.last_prompt_tokens,
            "estimated_prompt_tokens": s.last_estimated_prompt_tokens,
            "summarization_threshold_tokens": summarization_threshold_tokens,
            "context_window_tokens": context_window_tokens,
        }

    async def agent_usage(
        self, *, since: str | None, bucket: UsageGranularity | None = None
    ) -> dict[str, Any]:
        granularity: UsageGranularity = bucket or "day"
        since_ms = self._parse_since(since)
        try:
            s = await self._store.summary(thread_id=None, since_ms=since_ms)
            b = await self._store.breakdown(since_ms=since_ms, bucket=granularity)
        except Exception:
            logger.exception("agent usage query failed %s", kv(since=since, bucket=bucket))
            raise
        return {
            "turns": s.turns,
            "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens,
            "cached_tokens": s.cached_tokens,
            "cache_read_tokens": s.cache_read_tokens,
            "cache_creation_tokens": s.cache_creation_tokens,
            "cost_usd": s.cost_usd,
            "period_start": s.period_start_ms if s.period_start_ms is not None else 0,
            "period_end": s.period_end_ms if s.period_end_ms is not None else 0,
            "by_model": [asdict(row) for row in b.by_model],
            "by_day": [asdict(row) for row in b.by_day],
            "by_bucket_model": [asdict(row) for row in b.by_bucket_model],
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
        return SessionUsageResponse(
            session_id=session_id,
            last_prompt_tokens=0,
            estimated_prompt_tokens=0,
            summarization_threshold_tokens=max(1, int(cw * SUMMARY_TRIGGER_RATIO)),
            context_window_tokens=cw,
        ).model_dump()

    async def agent_usage(
        self, *, since: str | None, bucket: UsageGranularity | None = None
    ) -> dict[str, Any]:
        del since, bucket
        return AgentUsageResponse().model_dump()


def _content_blocks_to_text(blocks: list[ContentBlock]) -> str:
    """Transcript-only rendering of the incoming user turn (non-text blocks summarized)."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        else:
            parts.append(f"[{type(block).__name__}]")
    return "\n".join(parts)


def _default_agent_path(bus: SessionBus) -> Path:
    if bus.agent_md:
        return Path(bus.agent_md)
    env = os.environ.get("AGENT_MD")
    if not env:
        raise RuntimeError("AGENT_MD must be set when session has no agent_md path")
    return Path(env)


def _scripted_fake_provider() -> Provider:
    """Deterministic provider for ``MODEL_PROVIDER=fake`` (tests and local dev)."""
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


class GatewayLoopPort:
    """Schedules :func:`~monkeybot.core.runtime.loop.run` and forwards events to the session bus."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry
        # Bound when this port is used by a non-module app (e.g. create_realtime_app).
        self._fastapi_app: FastAPI | None = None

    def bind_app(self, fastapi_app: FastAPI) -> None:
        """Point storage/memory lookups at the serving FastAPI app."""
        self._fastapi_app = fastapi_app

    def _serving_app(self) -> FastAPI:
        return self._fastapi_app if self._fastapi_app is not None else app

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> None:
        bus = self._registry.get(session_id)
        if bus is None:
            return

        mcp = _deps.mcp
        inspectors = _deps.inspectors
        provider = bus.provider or _deps.provider

        if mcp is None or provider is None:
            logger.error("gateway deps not initialized")
            await bus.publish_data(
                event_to_json(AgentError(request_id=request_id, error="gateway_not_ready"))
            )
            await bus.publish_data(
                event_to_json(TurnComplete(request_id=request_id, usage=UsageTotals()))
            )
            return

        serving = self._serving_app()
        backend: StorageBackend = serving.state.storage
        history = backend.history()
        usage_store = backend.usage()

        cancel_event = asyncio.Event()
        bus.turn_cancel_event = cancel_event

        async def _watch_cancel() -> None:
            while True:
                if bus.cancel_requested_for == request_id:
                    cancel_event.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(_watch_cancel())

        executor: CoreToolExecutor | None = None
        try:
            model_name = bus.model_name or os.environ.get("MODEL_NAME", "gemini-2.5-flash")
            agent_path = _default_agent_path(bus)

            workspace_root, skills_resolved, artifacts_resolved = _resolved_workspace_paths()

            transcript_writer: TranscriptWriter | None = None
            if transcript_enabled_from_config():
                if bus.transcript_writer is None:
                    bus.transcript_writer = TranscriptWriter(
                        session_id, workspace_root=workspace_root
                    )
                transcript_writer = bus.transcript_writer
                await transcript_writer.ensure_manifest(
                    **runtime_manifest_fields(
                        model=model_name,
                        provider=provider.name,
                        workspace_root=str(workspace_root),
                        agent_md=str(agent_path),
                        context_window_tokens=_env_context_window_tokens(),
                        memory_on=getattr(serving.state, "memory", None) is not None,
                        mcp_catalog=mcp.catalog_names(),
                    ),
                )

            attachment_store: AttachmentStore | None = getattr(
                serving.state, "attachment_store", None
            )
            if bus.attachment_catalog is not None:
                rows = await history.load(session_id)
                bus.attachment_catalog.rebuild_from_history(rows)

            extra_tools: list[Any] = (
                [_deps.web_search_tool] if _deps.web_search_tool is not None else []
            )
            extra_tools.extend(_deps.computer_tools)
            todo_store = None
            if todo_list_enabled_from_env():
                if bus.todo_store is None:
                    bus.todo_store = TodoListStore(session_id, workspace_root=workspace_root)
                    await bus.todo_store.hydrate_from_disk()
                todo_store = bus.todo_store
                extra_tools.append(TodoListTool(todo_store))

            storage_backend = getattr(serving.state, "storage", None)
            loops_available = storage_backend is not None
            loops_advertised = loops_available and _deps.loops_registry.advertised

            try:
                ctx = await build_context(
                    session_id,
                    request_id,
                    agent_md_path=agent_path,
                    memory=getattr(serving.state, "memory", None),
                    skills_path=skills_resolved,
                    mcp_client=mcp,
                    model=model_name,
                    cancelled=cancel_event,
                    context_window_tokens=_env_context_window_tokens(),
                    workspace_root=workspace_root,
                    sse_bus=bus,
                    event_publisher=_SessionBusEventPublisher(bus),
                    extra_tools=extra_tools,
                    subagent_registry=_deps.subagent_registry,
                    scheduled_loops_available=loops_available,
                    loops_advertised=loops_advertised,
                    todo_store=todo_store,
                    approvals_persist=_deps.computer_approvals_persist,
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
                memory=getattr(serving.state, "memory", None),
                knowledge=getattr(serving.state, "knowledge", None),
                skills_path=skills_resolved,
                artifacts_path=artifacts_resolved,
                mcp=mcp,
                extra_tools=extra_tools,
                run_command_allowed_commands=_deps.run_command_allowed_commands,
                run_command_allowed_path_prefixes=_deps.run_command_allowed_path_prefixes,
                attachment_store=attachment_store,
                run_store=storage_backend.runs() if storage_backend is not None else None,
                scheduled_loop_store=(
                    storage_backend.scheduled_loops() if storage_backend is not None else None
                ),
                subagent_registry=_deps.subagent_registry,
                loops_registry=_deps.loops_registry,
            )
            if transcript_writer is not None:
                await transcript_writer.write_user_message(
                    request_id=request_id,
                    content=_content_blocks_to_text(user_content),
                )
            async for evt in run_loop(
                user_content,
                ctx,
                provider=provider,
                history=history,
                inspectors=inspectors,
                tool_executor=executor,
                cancelled=cancel_event,
                hook_manager=_deps.hook_manager,
                attachment_store=attachment_store,
                attachment_catalog=bus.attachment_catalog,
                transcript_writer=transcript_writer,
                vertex_google_search=vertex_google_search_enabled_from_config(),
                input_admission=bus.admission,
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
                            cache_read_tokens=u.cache_read_tokens,
                            cache_creation_tokens=u.cache_creation_tokens,
                            cost_usd=u.cost_usd,
                            duration_ms=u.duration_ms,
                            estimated_prompt_tokens=u.estimated_prompt_tokens,
                        ),
                        run_id=request_id,
                    )
                if transcript_writer is not None:
                    await transcript_writer.write_event(evt)
                await bus.publish_data(event_to_json(evt))
        finally:
            if executor is not None:
                await executor.aclose()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            bus.cancel_requested_for = None
            bus.turn_cancel_event = None


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


def _tool_denied_patterns() -> list[str]:
    """Substring deny list for :class:`RulesInspector`. Empty ``MONKEYBOT_TOOL_DENIED_PATTERNS`` disables."""
    raw = os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS")
    if raw is None:
        return ["rm -rf", "/etc/passwd", "DROP TABLE"]
    return [p.strip() for p in raw.split(",") if p.strip()]


async def _startup(fastapi_app: FastAPI) -> None:
    """Wire storage backend, MCP, inspectors, and provider."""
    # The root .env and YAML defaults must be available before any optional
    # initializer reads its environment (notably OpenTelemetry in ASGI mode).
    layout = ensure_gateway_runtime_env()
    log_gateway_startup(layout)

    from monkeybot.observability import init_observability

    otel_enabled = init_observability()
    if otel_enabled:
        logger.info("OpenTelemetry tracing initialized")
    else:
        logger.info("OpenTelemetry tracing not enabled")

    db_url = layout.db_url

    backend = create_storage_backend(
        db_url, agent_scope=layout.agent_id, agent_root=layout.agent_root
    )
    await backend.open(run_schema=auto_schema_enabled_from_config())
    fastapi_app.state.storage = backend
    fastapi_app.state.usage = _UsageStoreAdapter(backend.usage())

    mcp = MCPClient()
    _deps.mcp = mcp
    mcp_config = layout.mcp_config_path
    strict = os.environ.get("MCP_STRICT_LOAD", "").strip().lower() in ("1", "true", "yes")
    try:
        await mcp.load_from_config(mcp_config, raise_on_error=strict)
    except OSError as exc:
        logger.info("MCP config skipped (%s): %s", mcp_config, exc)

    tiers_path = layout.command_allowlist_path
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

    perm_path = layout.permission_config_path
    if should_enable_computer_tools():
        _deps.computer_tools = build_computer_tools()
        _deps.computer_approvals_persist = build_persist_hook(layout.approvals_path)
        inspectors.append(build_computer_permission_inspector(perm_path, layout.approvals_path))
    else:
        perm_insp = try_load_permission_inspector(perm_path)
        if perm_insp is not None:
            inspectors.append(perm_insp)

    inspectors.append(LoopStartInspector())
    _deps.inspectors = inspectors

    _deps.provider = _resolve_provider()

    vertex_gs = vertex_google_search_enabled_from_config()
    try:
        backend_ws = _build_web_search_backend()
        if backend_ws is not None:
            _deps.web_search_tool = WebSearchTool(backend_ws)
            logger.info("web search enabled: backend=%s", backend_ws.name)
        else:
            _deps.web_search_tool = None
    except Exception as exc:
        logger.warning("web search backend init failed — disabling: %s", exc)
        _deps.web_search_tool = None

    if vertex_gs:
        logger.info("vertex google_search grounding enabled")
    if _deps.web_search_tool is None and not vertex_gs:
        logger.info("web search disabled (WEB_SEARCH_BACKEND=none)")

    try:
        if not memory_enabled_from_config():
            logger.info("memory disabled (memory.enabled=false)")
            _deps.hook_manager = None
            _deps.memory = None
            fastapi_app.state.memory = None
            fastapi_app.state.memory_status = "disabled"
            fastapi_app.state.memory_detail = "memory.enabled=false"
        else:
            mem_uri = _memory_storage_uri()
            layout = AgentLayout.from_environment()
            mgr = HookManager()
            memory = MemorySubsystem(
                memory_uri=mem_uri,
                db_url=layout.db_url,
                agent_id=layout.agent_root.name,
                agent_name=layout.agent_root.name,
                storage=backend,
            )
            await memory.ensure_ready()
            memory.register_hooks(mgr)
            _deps.hook_manager = mgr
            _deps.memory = memory
            fastapi_app.state.memory = memory
            fastapi_app.state.memory_status = "enabled"
            fastapi_app.state.memory_detail = None
            logger.info("memory enabled (memory_storage_uri=%s)", mem_uri)
    except MemoryConfigurationError:
        raise
    except Exception as exc:
        logger.warning("memory setup failed; continuing without: %r", exc)
        _deps.hook_manager = None
        _deps.memory = None
        fastapi_app.state.memory = None
        fastapi_app.state.memory_status = "unavailable"
        fastapi_app.state.memory_detail = str(exc)

    # Unified knowledge layer — FTS + ANN + links + search
    if knowledge_enabled_from_config():
        try:
            if knowledge_read_only_from_env():
                logger.warning(
                    "MONKEYBOT_KNOWLEDGE_READ_ONLY is set but ignored here: the gateway "
                    "process is the sole writer per workspace and always opens the "
                    "knowledge index read-write. Set it on subagent/harness-as-library "
                    "clients instead."
                )
            layout = AgentLayout.from_environment()
            settings = resolve_knowledge_settings(workspace_root=layout.workspace_root)
            knowledge = await KnowledgeSubsystem.create(
                workspace_root=layout.workspace_root,
                settings=settings,
                knowledge_root=Path(settings.knowledge_root),
                index_path=Path(settings.index_path),
                read_only=False,
            )
            hook_mgr = _deps.hook_manager
            if hook_mgr is None:
                hook_mgr = HookManager()
                _deps.hook_manager = hook_mgr
            knowledge.register_hooks(hook_mgr)
            _deps.knowledge = knowledge
            fastapi_app.state.knowledge = knowledge

            async def _knowledge_startup_scan() -> None:
                try:
                    await knowledge.ensure_ready()
                    logger.info("knowledge index ready (path=%s)", settings.index_path)
                except Exception as scan_exc:
                    logger.warning("knowledge startup scan failed: %r", scan_exc)

            asyncio.create_task(_knowledge_startup_scan())
            logger.info("knowledge layer enabled (index=%s)", settings.index_path)
        except Exception as exc:
            logger.warning("knowledge layer setup failed; continuing without: %r", exc)
            _deps.knowledge = None
            fastapi_app.state.knowledge = None
    else:
        logger.info("knowledge layer disabled via knowledge.enabled")
        fastapi_app.state.knowledge = None

    if attachments_enabled_from_env():
        try:
            fastapi_app.state.attachment_store = FilesystemAttachmentStore(
                AgentLayout.from_environment().workspace_root
            )
            logger.info("attachments enabled")
        except Exception as exc:
            logger.warning("attachment store init failed: %s", exc)
            fastapi_app.state.attachment_store = None
    else:
        fastapi_app.state.attachment_store = None
        logger.info("attachments disabled via ATTACHMENTS_ENABLED")

    try:
        _deps.subagent_registry = get_subagent_registry()
        if _deps.subagent_registry:
            logger.info(
                "subagent personas loaded: %s",
                ", ".join(sorted(_deps.subagent_registry)),
            )
    except ConfigError as exc:
        logger.error("invalid subagents config: %s", exc)
        raise

    if otel_enabled:
        from monkeybot.observability.instrumentation import instrument_fastapi_app

        instrument_fastapi_app(fastapi_app)

    if os.environ.get("MONKEYBOT_WORKER_POOL", "").strip().lower() in ("1", "true", "yes"):
        # Development-only: runs the subagent worker loop on the gateway's own asyncio
        # event loop, so it competes with SSE streaming under load and has no backpressure.
        # For production, run standalone worker processes (`python -m monkeybot.subagents.worker`)
        # that scale independently of the gateway.
        from monkeybot.core.subagents.worker_pool import (
            start_worker_pool_background,
        )

        logger.warning(
            "MONKEYBOT_WORKER_POOL=1: running the subagent worker pool in-process is "
            "development-only — it shares the gateway event loop with SSE streaming. "
            "For production, run standalone workers via `python -m monkeybot.subagents.worker`."
        )
        fastapi_app.state.worker_pool = start_worker_pool_background(backend)

    from monkeybot.gateway.sse.scheduler_wiring import (
        GatewaySessionEnsurer,
        GatewayTickInvoker,
        StorageSessionBusyChecker,
    )
    from monkeybot.scheduler.engine import scheduler_enabled_from_env, start_scheduler_background

    if scheduler_enabled_from_env():
        loop_port = GatewayLoopPort(_registry)
        turn_locks = backend.session_turns()
        fastapi_app.state.scheduler = start_scheduler_background(
            store=backend.scheduled_loops(),
            invoker=GatewayTickInvoker(loop_port, _registry, turn_locks),
            session_busy=StorageSessionBusyChecker(turn_locks),
            ensure_session=GatewaySessionEnsurer(_registry),
        )
        logger.info("scheduled-loop engine enabled (in-process; development-friendly)")


async def _shutdown(fastapi_app: FastAPI) -> None:
    """Tear down MCP sessions and storage backend."""
    from monkeybot.observability import shutdown_observability

    try:
        shutdown_observability()
    except Exception as exc:
        logger.warning("observability shutdown failed: %s", exc)

    # Analyze any remaining session transcripts before the process exits.
    try:
        await _registry.remove_all_async()
    except Exception:
        logger.warning("transcript analysis on shutdown failed", exc_info=True)

    mcp = _deps.mcp
    if mcp is not None:
        for name in list(getattr(mcp, "_servers", {}).keys()):
            await mcp.disconnect(name)

    knowledge = _deps.knowledge or getattr(fastapi_app.state, "knowledge", None)
    if knowledge is not None:
        try:
            await knowledge.close()
        except Exception as exc:
            logger.warning("knowledge close failed: %s", exc)
        _deps.knowledge = None
        try:
            fastapi_app.state.knowledge = None
        except Exception as exc:
            logger.warning("knowledge state clear failed: %s", exc)

    memory = _deps.memory or getattr(fastapi_app.state, "memory", None)
    if memory is not None:
        try:
            await memory.close()
        except Exception as exc:
            logger.warning("memory close failed: %s", exc)
        _deps.memory = None
        try:
            fastapi_app.state.memory = None
        except Exception as exc:
            logger.warning("memory state clear failed: %s", exc)
    worker_pool_handle = getattr(fastapi_app.state, "worker_pool", None)
    if worker_pool_handle is not None:
        from monkeybot.core.subagents.worker_pool import shutdown_worker_pool

        await shutdown_worker_pool(worker_pool_handle)
        fastapi_app.state.worker_pool = None

    scheduler_handle = getattr(fastapi_app.state, "scheduler", None)
    if scheduler_handle is not None:
        from monkeybot.scheduler.engine import shutdown_scheduler

        await shutdown_scheduler(scheduler_handle)
        fastapi_app.state.scheduler = None

    storage: StorageBackend | None = getattr(fastapi_app.state, "storage", None)
    if storage is not None:
        await storage.close()


@contextlib.asynccontextmanager
async def _gateway_lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    await _startup(fastapi_app)
    try:
        yield
    finally:
        await _shutdown(fastapi_app)


_registry = SessionRegistry(workspace_root=_resolved_workspace_paths()[0])
app = build_sse_app(
    registry=_registry,
    loop_port=GatewayLoopPort(_registry),
    usage_port=_StaticUsagePortZeros(),
    lifespan=_gateway_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


def create_app() -> FastAPI:
    """Return the module-level ASGI app (for symmetry with docs/tests)."""
    return app
