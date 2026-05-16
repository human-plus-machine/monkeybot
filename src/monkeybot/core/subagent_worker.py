"""Subprocess worker for the ``task`` tool: stdin :class:`SubagentEnvelope` JSON, stdout NDJSON events."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

from monkeybot.core.context import build_context
from monkeybot.core.core_tool_executor import CoreToolExecutor
from monkeybot.core.db import apply_schema, open_connection
from monkeybot.core.events import Error, event_to_json
from monkeybot.core.history import ConversationHistory
from monkeybot.core.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.loop import run as run_loop
from monkeybot.core.mcp_client import MCPClient
from monkeybot.core.mocks_provider import ScriptedFakeProvider
from monkeybot.core.provider import Done, Message, ProviderEvent, TextDelta, ToolCall, UsageEvent
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.subagent_proto import SubagentEnvelope
from monkeybot.web_search import WebSearchTool
from monkeybot.web_search import build_backend as _build_web_search_backend

logger = logging.getLogger(__name__)


class _HistoryAdapter:
    """SQLite-backed :class:`ConversationHistory` as the loop history port."""

    def __init__(self, inner: ConversationHistory) -> None:
        self._inner = inner

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        return await self._inner.load(thread_id, limit=limit)

    async def append(self, thread_id: str, message: Message) -> None:
        await self._inner.append(thread_id, message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        await self._inner.reset(thread_id, messages)


def _resolve_provider() -> GeminiProvider | ScriptedFakeProvider:
    mode = os.environ.get("MODEL_PROVIDER", "gemini").lower().strip()
    if mode != "fake":
        return GeminiProvider()

    import json

    raw = os.environ.get("MONKEYBOT_FAKE_PROVIDER_EVENTS", "")
    if not raw:
        return ScriptedFakeProvider(
            [
                TextDelta(text="subagent fake provider: set MONKEYBOT_FAKE_PROVIDER_EVENTS for scripted tools."),
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

    ws = Path(os.environ["MONKEYBOT_SUBAGENT_WORKSPACE"]).resolve()
    os.chdir(ws)
    load_dotenv()

    mem = Path(os.environ["MONKEYBOT_SUBAGENT_MEMORY_PATH"]).resolve()
    skills = Path(os.environ["MONKEYBOT_SUBAGENT_SKILLS_PATH"]).resolve()

    agent_raw = os.environ.get("MONKEYBOT_SUBAGENT_AGENT_MD") or os.environ.get("AGENT_MD", "AGENT.md")
    agent_md_path = Path(agent_raw)
    if not agent_md_path.is_absolute():
        agent_md_path = (ws / agent_md_path).resolve()

    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    conn: aiosqlite.Connection | None = None
    mcp: MCPClient | None = None

    try:
        conn = await open_connection(db_url)
        await apply_schema(conn)

        mcp = MCPClient()
        mcp_config = Path(os.environ.get("MCP_CONFIG", "monkeybot_config/mcp.json"))
        try:
            await mcp.load_from_config(mcp_config)
        except OSError as exc:
            logger.info("MCP config skipped (%s): %s", mcp_config, exc)

        inspectors: list[ToolInspector] = []
        run_allow_cmds: list[str] | None = None
        run_allow_paths: list[str] | None = None
        tiers_path = Path(
            os.environ.get("COMMAND_ALLOWLIST_CONFIG", "monkeybot_config/command_allowlist.yaml")
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
            _ws_tool: WebSearchTool | None = WebSearchTool(_ws_backend) if _ws_backend is not None else None
        except Exception:
            _ws_tool = None

        extra_tools = [_ws_tool] if _ws_tool is not None else []

        ctx = await build_context(
            thread_id,
            request_id,
            agent_md_path=agent_md_path,
            memory_path=mem,
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
            memory_path=mem,
            skills_path=skills,
            mcp=mcp,
            extra_tools=extra_tools,
            run_command_allowed_commands=run_allow_cmds,
            run_command_allowed_path_prefixes=run_allow_paths,
        )
        history = _HistoryAdapter(ConversationHistory(conn))

        body = envelope.task.strip()
        if envelope.context.strip():
            body += "\n\n---\nContext from parent agent:\n" + envelope.context.strip()

        max_turns_raw = os.environ.get("SUBAGENT_MAX_TURNS", "").strip()
        if max_turns_raw:
            max_turns = max(1, int(max_turns_raw))
        else:
            max_turns = max(1, int(os.environ.get("MAX_TURNS", "25")))

        async for evt in run_loop(
            body,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=executor,
            run_id=request_id,
            cancelled=None,
            max_turns=max_turns,
        ):
            print(event_to_json(evt), flush=True)
    finally:
        await executor.aclose()
        if mcp is not None:
            for name in list(getattr(mcp, "_servers", {}).keys()):
                await mcp.disconnect(name)
        if conn is not None:
            await conn.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
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
