"""Regression cases for hiding an idle MCP server from one chat (T2-MCP-20..24)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext, build_context
from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.llm.realtime_provider import RealtimeToolCall
from monkeybot.core.mcp.session_visibility import IDLE_INNER_TURNS, MCPSessionVisibility
from monkeybot.core.prompts.prompt import latest_user_message_text
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.realtime_loop import run_realtime_turn
from monkeybot.core.types.content_blocks import Text, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import ToolsRecordingProvider


class _SharedMCP:
    """Connected server that records disconnects. Hiding must not call it."""

    def __init__(self) -> None:
        self.disconnected: list[str] = []
        self._tools = [ToolDef("browser__goto", "Go", {"type": "object"})]

    def known_server_names(self) -> list[str]:
        return ["browser"]

    def catalog_names(self) -> list[str]:
        return ["browser"]

    def is_connected(self, name: str) -> bool:
        return name == "browser"

    def all_tools(self) -> list[ToolDef]:
        return list(self._tools)

    async def disconnect(self, name: str) -> None:
        self.disconnected.append(name)

    async def wait_until_ready(self) -> None:
        return None


class _Exec(RecordingExecutor):
    def __init__(self, mcp: _SharedMCP) -> None:
        super().__init__(("ok", None))
        self.mcp = mcp


def _visibility_at(count: int) -> MCPSessionVisibility:
    visibility = MCPSessionVisibility()
    for _ in range(count):
        visibility.note_inner_turn({"browser"}, set())
    return visibility


def replace_window(ctx: TurnContext) -> TurnContext:
    return TurnContext(
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        agent_md=ctx.agent_md,
        memory_index=list(ctx.memory_index),
        skills=list(ctx.skills),
        tools=list(ctx.tools),
        user_id=ctx.user_id,
        parent_run_id=ctx.parent_run_id,
        model=ctx.model,
        mcp_visibility=ctx.mcp_visibility,
        catalog_mcp_servers=ctx.catalog_mcp_servers,
        context_window_tokens=20_000,
    )


def _ctx(visibility: MCPSessionVisibility) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("list_dir", "List", {}),
            ToolDef("browser__goto", "Go", {}),
        ],
        user_id=None,
        parent_run_id=None,
        model="fake",
        mcp_visibility=visibility,
        catalog_mcp_servers=("browser",),
    )


async def _run(ctx: TurnContext, provider: ToolsRecordingProvider, executor: _Exec) -> FakeHistory:
    history = FakeHistory()
    async for _event in run(
        "please list the directory",
        ctx,
        provider=provider,
        history=history,
        inspectors=[AllowInspector()],
        tool_executor=executor,
        max_turns=5,
    ):
        pass
    return history


def _tool_rows(history: FakeHistory) -> list[ToolResponse]:
    found: list[ToolResponse] = []
    for message in history.rows:
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, ToolResponse):
                found.append(block)
    return found


def _is_summary_request(messages: list[object]) -> bool:
    for message in messages:
        if getattr(message, "role", None) != "system":
            continue
        for block in message.content:
            if isinstance(block, Text) and block.text.startswith("Output exactly the Markdown"):
                return True
    return False


class _SummaryAwareProvider(ToolsRecordingProvider):
    """Compaction summaries do not consume the chat script."""

    async def stream(
        self, messages, tools, *, model, thinking_budget=None, vertex_google_search=False
    ):  # type: ignore[no-untyped-def]
        if _is_summary_request(list(messages)):
            self.stream_tools.append([tool.name for tool in tools])
            self.stream_messages.append(list(messages))
            yield TextDelta(text="compacted")
            yield Done()
            return
        async for event in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        ):
            yield event


def _fat_history() -> FakeHistory:
    return FakeHistory(
        [
            Message.text("user" if index % 2 == 0 else "assistant", f"row-{index}-" + ("Q" * 20000))
            for index in range(8)
        ]
    )


async def _run_until_compaction(
    ctx: TurnContext,
    provider: ToolsRecordingProvider,
    executor: _Exec,
) -> FakeHistory:
    history = _fat_history()
    async for _event in run(
        "please list the directory",
        ctx,
        provider=provider,
        history=history,
        inspectors=[AllowInspector()],
        tool_executor=executor,
        max_turns=6,
    ):
        pass
    return history


@pytest.mark.case("T2-MCP-20")
async def test_hiding_one_session_leaves_the_connection_and_other_sessions(
    tmp_path: Path,
) -> None:
    mcp = _SharedMCP()
    visibility = _visibility_at(IDLE_INNER_TURNS)
    ctx = _ctx(visibility)
    ctx = replace_window(ctx)
    provider = _SummaryAwareProvider([[TextDelta(text="done"), Done()]])
    await _run_until_compaction(ctx, provider, _Exec(mcp))
    assert visibility.is_hidden("browser")
    assert mcp.is_connected("browser")
    assert mcp.disconnected == []
    chat_tools = [
        tools
        for tools, messages in zip(provider.stream_tools, provider.stream_messages, strict=True)
        if not _is_summary_request(messages) and tools
    ]
    assert chat_tools
    assert "browser__goto" not in chat_tools[-1]

    agent = tmp_path / "AGENT.md"
    agent.write_text("You are a helpful assistant.\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    other = await build_context(
        "thread-b",
        "req-b",
        session_id="session-b",
        agent_md_path=agent,
        memory=None,
        skills_path=skills,
        mcp_client=mcp,
        mcp_visibility=MCPSessionVisibility(),
    )
    assert any(tool.name == "browser__goto" for tool in other.tools)


@pytest.mark.case("T2-MCP-21")
async def test_idle_note_stays_inside_the_tool_result() -> None:
    visibility = _visibility_at(IDLE_INNER_TURNS)
    provider = _SummaryAwareProvider(
        [
            [ToolCall(name="list_dir", args={"path": "."}, call_id="c1"), Done()],
            [TextDelta(text="done"), Done()],
        ]
    )
    history = await _run_until_compaction(
        replace_window(_ctx(visibility)), provider, _Exec(_SharedMCP())
    )
    assert latest_user_message_text(history.rows) == "please list the directory"
    responses = _tool_rows(history)
    assert responses
    note_row = next(
        message
        for message in history.rows
        if any(isinstance(block, ToolResponse) for block in message.content)
        and any(
            isinstance(block, ToolResponse)
            and any(isinstance(item, Text) and "went unused" in item.text for item in block.result)
            for block in message.content
        )
    )
    assert not any(isinstance(block, Text) for block in note_row.content)
    note = "".join(
        block.text for response in responses for block in response.result if isinstance(block, Text)
    )
    assert "because it went unused" in note
    assert "enable_mcp" in note


@pytest.mark.case("T2-MCP-22")
async def test_no_tool_recovery_turn_does_not_count_as_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "3")
    visibility = _visibility_at(2)
    same = ToolCall(name="list_dir", args={"path": "."}, call_id="c1")
    provider = ToolsRecordingProvider(
        [
            [
                same,
                ToolCall(name="list_dir", args={"path": "."}, call_id="c2"),
                ToolCall(name="list_dir", args={"path": "."}, call_id="c3"),
                Done(),
            ],
            [TextDelta(text="stopped"), Done()],
        ]
    )
    await _run(_ctx(visibility), provider, _Exec(_SharedMCP()))
    assert visibility.idle_count("browser") == 3
    assert not visibility.is_hidden("browser")
    assert provider.stream_tools[-1] == []


@pytest.mark.case("T2-MCP-23")
async def test_aborted_batch_does_not_hide() -> None:
    class _Cancel(_Exec):
        async def execute(self, *, call: ToolCall, ctx: TurnContext):  # type: ignore[no-untyped-def]
            self.calls.append(call)
            del ctx
            raise asyncio.CancelledError()

    visibility = _visibility_at(IDLE_INNER_TURNS - 1)
    provider = ToolsRecordingProvider(
        [[ToolCall(name="list_dir", args={"path": "."}, call_id="c1"), Done()]]
    )
    history = await _run(_ctx(visibility), provider, _Cancel(_SharedMCP()))
    assert not visibility.is_hidden("browser")
    assert visibility.idle_count("browser") == IDLE_INNER_TURNS
    blob = " ".join(
        block.text
        for message in history.rows
        for block in message.content
        for block in (block.result if isinstance(block, ToolResponse) else [block])
        if isinstance(block, Text)
    )
    assert "was hidden" not in blob


@pytest.mark.case("T2-MCP-24")
async def test_voice_turn_does_not_hide_and_a_call_resets_the_count() -> None:
    visibility = _visibility_at(IDLE_INNER_TURNS)
    ctx = _ctx(visibility)
    executor = _Exec(_SharedMCP())
    async for _event in run_realtime_turn(
        "hello",
        "hi",
        [],
        ctx,
        history=FakeHistory(),
        tool_executor=executor,
    ):
        pass
    assert not visibility.is_hidden("browser")
    assert visibility.idle_count("browser") == IDLE_INNER_TURNS

    async for _event in run_realtime_turn(
        "open it",
        "",
        [RealtimeToolCall(call_id="c1", name="browser__goto", args={"url": "https://example.com"})],
        ctx,
        history=FakeHistory(),
        tool_executor=executor,
    ):
        pass
    assert not visibility.is_hidden("browser")
    assert visibility.idle_count("browser") == 0
