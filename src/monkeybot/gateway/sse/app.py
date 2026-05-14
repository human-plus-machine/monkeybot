"""Production FastAPI application wiring for the SSE gateway (Story 8).

Bootstraps SQLite, MCP, command tiers, and a real :func:`monkeybot.core.loop.run` driver;
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

import aiosqlite

from monkeybot.core.context import build_context
from monkeybot.core.core_tool_executor import CoreToolExecutor
from monkeybot.core.db import apply_schema, open_connection
from monkeybot.core.events import Error as AgentError
from monkeybot.core.events import TurnComplete, UsageTotals, event_to_json
from monkeybot.core.history import ChatMessage, ConversationHistory
from monkeybot.core.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.loop import run as run_loop
from monkeybot.core.mcp_client import MCPClient
from monkeybot.core.mocks_provider import ScriptedFakeProvider
from monkeybot.core.provider import Done, Message, Provider, TextDelta, ToolCall, UsageEvent
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider
from monkeybot.core.usage import Usage as UsageRecord
from monkeybot.core.usage import UsageStore
from monkeybot.gateway.sse.loop_port import UsagePort
from monkeybot.gateway.sse.routes import create_app as build_sse_app
from monkeybot.gateway.sse.session_bus import SessionBus, SessionRegistry
from fastapi import FastAPI

logger = logging.getLogger(__name__)


@dataclass
class _GatewayDeps:
    """Process-level deps populated on startup (mutable module singleton)."""

    db_url: str | None = None
    mcp: MCPClient | None = None
    inspectors: list[ToolInspector] = field(default_factory=list)
    provider: Provider | None = None
    usage_conn: aiosqlite.Connection | None = None


_deps = _GatewayDeps()


def _env_context_window_tokens() -> int:
    cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "1000000").strip()
    try:
        return max(1, int(cap_raw))
    except ValueError:
        return 1_000_000


class _HistoryAdapter:
    """SQLite-backed :class:`ConversationHistory` exposed as the loop history port."""

    def __init__(self, inner: ConversationHistory) -> None:
        self._inner = inner

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        rows = await self._inner.load(thread_id, limit=limit)
        return [
            Message(
                role=r.role,
                content=r.content,
                tool_name=r.tool_name,
                tool_call_id=r.tool_call_id,
            )
            for r in rows
        ]

    async def append(self, thread_id: str, message: Message) -> None:
        await self._inner.append(
            thread_id,
            ChatMessage(
                role=message.role,
                content=message.content,
                tool_call_id=message.tool_call_id,
                tool_name=message.tool_name,
            ),
        )


def _resolved_workspace_paths() -> tuple[Path, Path, Path]:
    """Resolve workspace, memory, and skills roots relative to the process cwd."""
    root = Path.cwd().resolve()
    mem = Path(os.environ.get("MEMORY_PATH", "data/memory"))
    skills = Path(os.environ.get("SKILLS_PATH", "skills"))
    mem_p = mem.resolve() if mem.is_absolute() else (root / mem).resolve()
    skills_p = skills.resolve() if skills.is_absolute() else (root / skills).resolve()
    return root, mem_p, skills_p


class _UsageStoreAdapter(UsagePort):
    """GET /usage backed by :class:`UsageStore` (summary connection opened at startup)."""

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
        ps = s.get("period_start_ms")
        pe = s.get("period_end_ms")
        context_window_tokens = _env_context_window_tokens()
        return {
            "session_id": session_id,
            "turns": int(s["turns"]),
            "input_tokens": int(s["input_tokens"]),
            "output_tokens": int(s["output_tokens"]),
            "cached_tokens": int(s["cached_tokens"]),
            "cost_usd": float(s["cost_usd"]),
            "period_start": int(ps) if ps is not None else 0,
            "period_end": int(pe) if pe is not None else 0,
            "last_prompt_tokens": int(s.get("last_prompt_tokens", 0)),
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
            "context_window_tokens": _env_context_window_tokens(),
        }


def _default_agent_path(bus: SessionBus) -> Path:
    if bus.agent_md:
        return Path(bus.agent_md)
    env = os.environ.get("AGENT_MD")
    if not env:
        raise RuntimeError("AGENT_MD must be set when session has no agent_md path")
    return Path(env)


def _memory_path() -> Path:
    return Path(os.environ.get("MEMORY_PATH", "data/memory"))


def _skills_path() -> Path:
    return Path(os.environ.get("SKILLS_PATH", "skills"))


def _resolve_provider() -> Provider:
    mode = os.environ.get("MODEL_PROVIDER", "gemini").lower().strip()
    if mode == "vertex-claude":
        return VertexClaudeProvider()
    if mode != "fake":
        return GeminiProvider()

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
    turns: list[list[object]] = []
    for turn in decoded:
        events: list[object] = []
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
    return ScriptedFakeProvider(turns)


class GatewayLoopPort:
    """Schedules :func:`~monkeybot.core.loop.run` and forwards events to the session bus."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    async def start_turn(self, session_id: str, request_id: str, message: str) -> None:
        bus = self._registry.get(session_id)
        if bus is None:
            return

        db_url = _deps.db_url
        mcp = _deps.mcp
        inspectors = _deps.inspectors
        provider = _deps.provider

        if not db_url or mcp is None or provider is None:
            logger.error("gateway deps not initialized")
            await bus.publish_data(
                event_to_json(AgentError(request_id=request_id, error="gateway_not_ready"))
            )
            await bus.publish_data(
                event_to_json(TurnComplete(request_id=request_id, usage=UsageTotals()))
            )
            return

        conn: aiosqlite.Connection | None = None
        cancel_event = asyncio.Event()

        async def _watch_cancel() -> None:
            while True:
                if bus.cancel_requested_for == request_id:
                    cancel_event.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(_watch_cancel())

        try:
            conn = await open_connection(db_url)
            await _ensure_schema(conn)
            history = _HistoryAdapter(ConversationHistory(conn))
            usage_store = UsageStore(conn)

            model_name = os.environ.get("MODEL_NAME", "gemini-2.5-flash")
            agent_path = _default_agent_path(bus)

            try:
                ctx = await build_context(
                    session_id,
                    request_id,
                    agent_md_path=agent_path,
                    memory_path=_memory_path(),
                    skills_path=_skills_path(),
                    mcp_client=mcp,
                    model=model_name,
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

            workspace_root, memory_resolved, skills_resolved = _resolved_workspace_paths()
            executor = CoreToolExecutor(
                workspace_root=workspace_root,
                memory_path=memory_resolved,
                skills_path=skills_resolved,
                mcp=mcp,
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
                        ),
                        run_id=request_id,
                    )
                await bus.publish_data(event_to_json(evt))
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            bus.cancel_requested_for = None
            if conn is not None:
                await conn.close()


_registry = SessionRegistry()
app = build_sse_app(
    registry=_registry,
    loop_port=GatewayLoopPort(_registry),
    usage_port=_StaticUsagePortZeros(),
)


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    await apply_schema(conn)


def _tool_denied_patterns() -> list[str]:
    """Substring deny list for :class:`RulesInspector`. Empty ``MONKEYBOT_TOOL_DENIED_PATTERNS`` disables."""
    raw = os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS")
    if raw is None:
        return ["rm -rf", "/etc/passwd", "DROP TABLE"]
    return [p.strip() for p in raw.split(",") if p.strip()]


@app.on_event("startup")
async def _startup() -> None:
    """Wire MCP, SQLite usage reader, inspectors, and provider."""
    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    _deps.db_url = db_url

    mcp = MCPClient()
    _deps.mcp = mcp
    mcp_config = Path(os.environ.get("MCP_CONFIG", "/app/mcp.json"))
    try:
        await mcp.load_from_config(mcp_config)
    except OSError as exc:
        logger.info("MCP config skipped (%s): %s", mcp_config, exc)

    tiers_path = Path(os.environ.get("COMMAND_TIERS_CONFIG", "/app/config/command_tiers.yaml"))
    inspectors: list[ToolInspector] = []
    try:
        inspectors.append(CommandTierInspector(tiers_path))
    except FileNotFoundError:
        logger.info("command tiers missing (%s); allowing all tool calls", tiers_path)
    except Exception as exc:
        logger.exception("command tier load failed: %s", exc)

    denied = _tool_denied_patterns()
    if denied:
        inspectors.append(RulesInspector(denied))
    _deps.inspectors = inspectors

    _deps.provider = _resolve_provider()

    usage_conn = await open_connection(db_url)
    await _ensure_schema(usage_conn)
    _deps.usage_conn = usage_conn
    app.state.usage = _UsageStoreAdapter(UsageStore(usage_conn))


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Tear down MCP sessions and SQLite usage connection."""
    mcp = _deps.mcp
    if mcp is not None:
        for name in list(getattr(mcp, "_servers", {}).keys()):
            await mcp.disconnect(name)

    if _deps.usage_conn is not None:
        await _deps.usage_conn.close()
        _deps.usage_conn = None


def create_app() -> FastAPI:
    """Return the module-level ASGI app (for symmetry with docs/tests)."""
    return app
