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
from monkeybot.core.history import ChatMessage, ConversationHistory
from monkeybot.core.inspector import CommandTierInspector, RulesInspector, ToolInspector
from monkeybot.core.loop import run as run_loop
from monkeybot.core.mcp_client import MCPClient
from monkeybot.core.provider import Done, Message, TextDelta, ToolCall, UsageEvent
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.mocks_provider import ScriptedFakeProvider
from monkeybot.core.subagent_proto import SubagentEnvelope

logger = logging.getLogger(__name__)


class _HistoryAdapter:
    """SQLite-backed :class:`ConversationHistory` as the loop history port."""

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
        mcp_config = Path(os.environ.get("MCP_CONFIG", "mcp.json"))
        try:
            await mcp.load_from_config(mcp_config)
        except OSError as exc:
            logger.info("MCP config skipped (%s): %s", mcp_config, exc)

        inspectors: list[ToolInspector] = []
        tiers_path = Path(os.environ.get("COMMAND_TIERS_CONFIG", "config/command_tiers.yaml"))
        try:
            inspectors.append(CommandTierInspector(tiers_path))
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
        )

        executor = CoreToolExecutor(
            workspace_root=ws,
            memory_path=mem,
            skills_path=skills,
            mcp=mcp,
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
