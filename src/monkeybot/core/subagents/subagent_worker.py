"""Subprocess worker for the ``task`` tool: stdin :class:`SubagentEnvelope` JSON, stdout NDJSON events."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from monkeybot.core.config.settings import (
    auto_schema_enabled_from_config,
    get_provider_config,
    get_subagent_settings,
    normalize_model_provider,
    subagent_vertex_google_search_from_config,
)
from monkeybot.core.context import TurnContext, build_context
from monkeybot.core.knowledge import KnowledgeSubsystem, resolve_knowledge_settings
from monkeybot.core.knowledge.config import knowledge_enabled_from_config
from monkeybot.core.layout import AgentLayout, bootstrap_agent_layout
from monkeybot.core.llm.provider import (
    Done,
    Provider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import HistoryStore, create_storage_backend
from monkeybot.core.runtime.events import (
    AgentEvent,
    Error,
    SystemPromptSnapshot,
    event_to_json,
)
from monkeybot.core.runtime.loop import run as run_loop
from monkeybot.core.subagents.subagent_proto import (
    SubagentEnvelope,
    config_path_for_agent_root,
    resolve_agent_project_root,
    resolve_default_agent_md_path,
    resolve_project_path,
)
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.tools.permission import try_load_permission_inspector
from monkeybot.web_search import WebSearchTool
from monkeybot.web_search import build_backend as _build_web_search_backend

_BUILTIN_RUN_LOOP = run_loop

logger = logging.getLogger(__name__)


def _subagent_memory_uri(envelope: SubagentEnvelope) -> str:
    """Envelope URI is authoritative; empty means no memory (ignore ambient env)."""
    return envelope.memory_storage_uri.strip()


def init_observability() -> bool:
    from monkeybot.observability import init_observability as _init

    return _init()


def shutdown_observability() -> None:
    from monkeybot.observability import shutdown_observability as _shutdown

    _shutdown()


def _attach_trace_from_envelope(envelope: SubagentEnvelope) -> object | None:
    if not envelope.traceparent:
        return None
    try:
        from opentelemetry import context as otel_context

        from monkeybot.observability.propagation import extract_traceparent
    except ImportError:
        return None
    ctx = extract_traceparent({"traceparent": envelope.traceparent})
    if ctx is None:
        return None
    return otel_context.attach(ctx)


def _detach_trace(token: object | None) -> None:
    if token is None:
        return
    try:
        from opentelemetry import context as otel_context

        otel_context.detach(cast(Any, token))
    except Exception:
        logger.debug("trace detach failed", exc_info=True)
        return


def _reset_trace_context_if_unattached(attach_token: object | None) -> object | None:
    if attach_token is not None:
        return None
    try:
        from opentelemetry import context as otel_context

        return otel_context.attach(otel_context.Context())
    except ImportError:
        return None


def _clear_span_exporter_buffer() -> None:
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        multi = getattr(provider, "_active_span_processor", None)
        processors = getattr(multi, "_span_processors", ()) if multi is not None else ()
        for proc in processors:
            exporter = getattr(proc, "span_exporter", None)
            if exporter is not None and hasattr(exporter, "clear"):
                exporter.clear()
    except Exception:
        logger.debug("span exporter buffer clear failed", exc_info=True)
        return


async def _stream_run_loop_events(
    body: str,
    ctx: TurnContext,
    *,
    provider: Provider,
    history: HistoryStore,
    inspectors: list[ToolInspector],
    tool_executor: CoreToolExecutor,
    run_id: str,
    max_turns: int,
    vertex_google_search: bool = False,
) -> AsyncIterator[AgentEvent]:
    if run_loop is _BUILTIN_RUN_LOOP:
        async for evt in run_loop(
            body,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=tool_executor,
            cancelled=None,
            max_turns=max_turns,
            vertex_google_search=vertex_google_search,
        ):
            yield evt
        return

    from monkeybot.observability.spans import span_run

    async with span_run(ctx, user_message=body):
        async for evt in run_loop(
            body,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=tool_executor,
            cancelled=None,
            max_turns=max_turns,
            vertex_google_search=vertex_google_search,
        ):
            yield evt


def _event_for_ndjson_pipe(evt: AgentEvent) -> AgentEvent:
    """Shrink live-only payloads before writing to the parent NDJSON pipe.

    ``SystemPromptSnapshot.text`` includes the full memory INDEX when curation is
    off for subagents. Emitting that verbatim used to blow asyncio's 64 KiB
    ``readline`` limit on the parent. Parent drain ignores the snapshot anyway.
    """
    if isinstance(evt, SystemPromptSnapshot):
        return SystemPromptSnapshot(
            request_id=evt.request_id,
            inner_turn=evt.inner_turn,
            text=f"[omitted {len(evt.text)} chars]",
        )
    return evt


def _resolve_provider() -> Provider:
    mode = normalize_model_provider(os.environ.get("MODEL_PROVIDER", "google_vertexai"))
    if mode != "fake":
        return get_provider_config(provider=mode).provider

    import json

    raw = os.environ.get("MONKEYBOT_FAKE_PROVIDER_EVENTS", "")
    if not raw:
        return ScriptedFakeProvider(
            [
                TextDelta(
                    text="subagent fake provider: set MONKEYBOT_FAKE_PROVIDER_EVENTS for scripted tools."
                ),
                UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0),
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


async def _async_main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        print(event_to_json(Error(request_id="", error="subagent_worker: empty stdin")), flush=True)
        raise SystemExit(1)

    bootstrap_agent_layout()
    envelope = SubagentEnvelope.from_json(raw)
    attach_token: object | None = _attach_trace_from_envelope(envelope)
    reset_token: object | None = None

    agent_root = resolve_agent_project_root()
    config_path = config_path_for_agent_root(agent_root)
    ws = Path(os.environ["MONKEYBOT_SUBAGENT_WORKSPACE"]).resolve()
    os.chdir(ws)

    mem_uri = _subagent_memory_uri(envelope)

    skills = Path(os.environ["MONKEYBOT_SUBAGENT_SKILLS_PATH"]).resolve()
    artifacts_env = os.environ.get("MONKEYBOT_SUBAGENT_ARTIFACTS_PATH")
    artifacts = Path(artifacts_env).resolve() if artifacts_env else None

    if envelope.agent_md:
        agent_md_path = Path(envelope.agent_md).expanduser().resolve()
        if not agent_md_path.is_file():
            agent_md_path = resolve_project_path(envelope.agent_md, agent_root)
    else:
        try:
            agent_md_path = resolve_default_agent_md_path(agent_root)
        except ValueError:
            agent_md_path = resolve_project_path("AGENT.md", agent_root)

    layout = AgentLayout.from_environment(agent_root=agent_root)
    backend = create_storage_backend(
        layout.db_url, agent_scope=layout.agent_id, agent_root=layout.agent_root
    )
    mcp: MCPClient | None = None
    executor: CoreToolExecutor | None = None
    knowledge: KnowledgeSubsystem | None = None

    try:
        await backend.open(run_schema=auto_schema_enabled_from_config(config_path))

        mcp = MCPClient()
        mcp_config = resolve_project_path(
            os.environ.get("MCP_CONFIG", "monkeybot_config/mcp.json"), agent_root
        )
        strict = os.environ.get("MCP_STRICT_LOAD", "").strip().lower() in ("1", "true", "yes")
        try:
            await mcp.load_from_config(mcp_config, raise_on_error=strict)
        except OSError as exc:
            logger.info("MCP config skipped (%s): %s", mcp_config, exc)

        inspectors: list[ToolInspector] = []
        run_allow_cmds: list[str] | None = None
        run_allow_paths: list[str] | None = None
        tiers_path = resolve_project_path(
            os.environ.get("COMMAND_ALLOWLIST_CONFIG", "monkeybot_config/command_allowlist.yaml"),
            agent_root,
        )
        try:
            tier_insp = CommandTierInspector(tiers_path)
            inspectors.append(tier_insp)
            run_allow_cmds = list(tier_insp.allowed_commands)
            run_allow_paths = list(tier_insp.allowed_path_prefixes)
        except FileNotFoundError:
            logger.info("command tiers missing (%s); allowing all tool calls", tiers_path)
        except Exception:
            logger.exception("command tier load failed")

        raw_rules = os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS")
        if raw_rules is None:
            rules_patterns = ["rm -rf", "/etc/passwd", "DROP TABLE"]
        else:
            rules_patterns = [p.strip() for p in raw_rules.split(",") if p.strip()]
        if rules_patterns:
            inspectors.append(RulesInspector(rules_patterns))

        perm_path = resolve_project_path(
            os.environ.get("PERMISSION_CONFIG", "monkeybot_config/permissions.yaml"),
            agent_root,
        )
        # Subagents have no interactive session to prompt: reuse the parent's
        # ruleset, but resolve "ask" to deny instead of a confirm that can
        # never be answered.
        perm_insp = try_load_permission_inspector(perm_path, allow_ask=False)
        if perm_insp is not None:
            inspectors.append(perm_insp)

        provider = _resolve_provider()
        # Prefer parent-allocated id so SSE progress and the child transcript share one key.
        # Otherwise namespace spill dirs under the parent chat session so session-end
        # cleanup can remove ``.monkeybot/spill/subagent:{session_id}:*``.
        if envelope.child_thread_id:
            thread_id = envelope.child_thread_id
        else:
            spill_session = envelope.parent_session_id or envelope.parent_run_id
            thread_id = f"subagent:{spill_session}:{uuid.uuid4().hex[:10]}"
        request_id = f"sub-{uuid.uuid4().hex[:12]}"

        cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "200000").strip()
        try:
            context_window_tokens = max(1, int(cap_raw))
        except ValueError:
            context_window_tokens = 200_000

        try:
            _ws_backend = _build_web_search_backend()
            _ws_tool: WebSearchTool | None = (
                WebSearchTool(_ws_backend) if _ws_backend is not None else None
            )
        except Exception:
            logger.debug("web search backend init failed", exc_info=True)
            _ws_tool = None

        extra_tools = [_ws_tool] if _ws_tool is not None else []

        memory: MemorySubsystem | None = None
        if mem_uri:
            memory = MemorySubsystem(
                memory_uri=mem_uri,
                db_url=os.environ.get("DB_URL", "sqlite:///data/monkeybot.db"),
                agent_id=agent_root.name,
                agent_name=agent_root.name,
                ingest_enabled=False,
                writer_enabled=False,
            )

        # Read-only knowledge search against the parent gateway's index.
        # Subagents must not claim the writer lock or run indexing/hooks.
        if knowledge_enabled_from_config(config_path):
            try:
                settings = resolve_knowledge_settings(
                    agent_root=agent_root,
                    config_path=Path(config_path) if config_path else None,
                    workspace_root=ws,
                )
                knowledge = await KnowledgeSubsystem.create(
                    workspace_root=ws,
                    settings=settings,
                    knowledge_root=Path(settings.knowledge_root),
                    index_path=Path(settings.index_path),
                    read_only=True,
                )
            except FileNotFoundError as exc:
                logger.info("knowledge read-only open skipped (index not ready yet): %s", exc)
                knowledge = None
            except Exception as exc:
                logger.warning("knowledge read-only setup failed for subagent: %r", exc)
                knowledge = None

        ctx = await build_context(
            thread_id,
            request_id,
            agent_md_path=agent_md_path,
            memory=memory,
            skills_path=skills,
            mcp_client=mcp,
            parent_run_id=envelope.parent_run_id,
            model=envelope.model,
            include_task_tool=False,
            workspace_root=ws,
            context_window_tokens=context_window_tokens,
            enable_context_curation=False,
            extra_tools=extra_tools,
        )

        executor = CoreToolExecutor(
            workspace_root=ws,
            memory=memory,
            skills_path=skills,
            artifacts_path=artifacts,
            mcp=mcp,
            extra_tools=extra_tools,
            run_command_allowed_commands=run_allow_cmds,
            run_command_allowed_path_prefixes=run_allow_paths,
            knowledge=knowledge,
        )
        history = backend.history()

        body = envelope.task.strip()
        if envelope.context.strip():
            body += "\n\n---\nContext from parent agent:\n" + envelope.context.strip()

        max_turns = get_subagent_settings(config_path).max_turns

        from monkeybot.observability.spans import span_subagent

        reset_token = _reset_trace_context_if_unattached(attach_token)
        _clear_span_exporter_buffer()
        init_observability()
        try:
            # Subagents read palace wake-up via MemorySubsystem but do not
            # register ingest hooks or start a writer — parent owns automatic capture.
            # Knowledge search is read-only against the parent index (no indexer/hooks).
            async with span_subagent(
                thread_id=thread_id,
                request_id=request_id,
                parent_run_id=envelope.parent_run_id,
                subagent_type=envelope.subagent_type,
                agent_md=envelope.agent_md,
            ):
                async for evt in _stream_run_loop_events(
                    body,
                    ctx,
                    provider=provider,
                    history=history,
                    inspectors=inspectors,
                    tool_executor=executor,
                    run_id=request_id,
                    max_turns=max_turns,
                    vertex_google_search=subagent_vertex_google_search_from_config(config_path),
                ):
                    print(event_to_json(_event_for_ndjson_pipe(evt)), flush=True)
        finally:
            shutdown_observability()
    finally:
        _detach_trace(reset_token)
        _detach_trace(attach_token)
        if executor is not None:
            await executor.aclose()
        if knowledge is not None:
            try:
                await knowledge.close()
            except Exception as exc:
                logger.warning("knowledge close failed in subagent: %r", exc)
        if mcp is not None:
            for name in list(getattr(mcp, "_servers", {}).keys()):
                await mcp.disconnect(name)
        await backend.close()


def main() -> None:
    from monkeybot.core.logging_utils import normalize_log_level

    logging.basicConfig(level=normalize_log_level(os.environ.get("LOG_LEVEL"), default="WARNING"))
    try:
        asyncio.run(_async_main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logger.exception("subagent_worker fatal")
        print(event_to_json(Error(request_id="", error=str(exc))), flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
