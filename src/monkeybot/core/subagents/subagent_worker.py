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

from dotenv import load_dotenv

from monkeybot.core.config.settings import get_provider_config, normalize_model_provider
from monkeybot.core.context import TurnContext, build_context
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
from monkeybot.core.runtime.events import AgentEvent, Error, event_to_json
from monkeybot.core.runtime.loop import run as run_loop
from monkeybot.core.subagents.subagent_proto import (
    SubagentEnvelope,
    normalize_sqlite_db_url,
    resolve_agent_project_root,
    resolve_project_path,
    resolve_subagent_agent_md_path,
)
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.workspace import create_workspace_storage
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
) -> AsyncIterator[AgentEvent]:
    if run_loop is _BUILTIN_RUN_LOOP:
        async for evt in run_loop(
            body,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=tool_executor,
            run_id=run_id,
            cancelled=None,
            max_turns=max_turns,
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
            run_id=run_id,
            cancelled=None,
            max_turns=max_turns,
        ):
            yield evt


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

    envelope = SubagentEnvelope.from_json(raw)
    attach_token: object | None = _attach_trace_from_envelope(envelope)
    reset_token: object | None = None

    agent_root = resolve_agent_project_root()
    dotenv_path = agent_root / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path)

    ws = Path(os.environ["MONKEYBOT_SUBAGENT_WORKSPACE"]).resolve()
    os.chdir(ws)

    mem_uri = _subagent_memory_uri(envelope)

    skills = Path(os.environ["MONKEYBOT_SUBAGENT_SKILLS_PATH"]).resolve()

    agent_md_path = resolve_subagent_agent_md_path(agent_root) or resolve_project_path(
        "AGENT.md", agent_root
    )

    db_url = normalize_sqlite_db_url(
        os.environ.get("DB_URL", "sqlite:///data/monkeybot.db"), agent_root
    )
    backend = create_storage_backend(db_url)
    mcp: MCPClient | None = None
    executor: CoreToolExecutor | None = None

    try:
        await backend.open()

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

        provider = _resolve_provider()
        thread_id = f"subagent:{envelope.parent_run_id}:{uuid.uuid4().hex[:10]}"
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
            storage = create_workspace_storage(mem_uri)
            memory = MemorySubsystem(
                storage=storage,
                provider=provider,
                model=envelope.model,
                memory_uri=mem_uri,
            )

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
            mcp=mcp,
            extra_tools=extra_tools,
            run_command_allowed_commands=run_allow_cmds,
            run_command_allowed_path_prefixes=run_allow_paths,
        )
        history = backend.history()

        body = envelope.task.strip()
        if envelope.context.strip():
            body += "\n\n---\nContext from parent agent:\n" + envelope.context.strip()

        max_turns_raw = os.environ.get("SUBAGENT_MAX_TURNS", "").strip()
        if max_turns_raw:
            max_turns = max(1, int(max_turns_raw))
        else:
            max_turns = max(1, int(os.environ.get("MAX_TURNS", "25")))

        from monkeybot.observability.spans import span_subagent

        reset_token = _reset_trace_context_if_unattached(attach_token)
        _clear_span_exporter_buffer()
        init_observability()
        try:
            # Subagents read memory (index, search_memory) via MemorySubsystem but do not
            # register memory hooks — chat_log / raw / organizer writes stay on the parent
            # gateway to avoid duplicate or conflicting durable memory updates.
            async with span_subagent(
                thread_id=thread_id,
                request_id=request_id,
                parent_run_id=envelope.parent_run_id,
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
                ):
                    print(event_to_json(evt), flush=True)
        finally:
            shutdown_observability()
    finally:
        _detach_trace(reset_token)
        _detach_trace(attach_token)
        if executor is not None:
            await executor.aclose()
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
