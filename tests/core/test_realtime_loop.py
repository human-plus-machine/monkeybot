"""Tests for ``monkeybot.core.runtime.realtime_loop``."""

from __future__ import annotations

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Message, ToolCall
from monkeybot.core.llm.realtime_provider import RealtimeToolCall
from monkeybot.core.runtime.events import (
    AssistantDelta,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
)
from monkeybot.core.runtime.realtime_loop import run_realtime_turn
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef


class FakeHistory:
    def __init__(self) -> None:
        self.rows: list[Message] = []

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        del thread_id, limit
        return list(self.rows)

    async def append(self, thread_id: str, message: Message) -> None:
        del thread_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id, messages


class RecordingExecutor:
    def __init__(self, result: ToolExecutionResult | None = None) -> None:
        self.result = result if result is not None else ToolExecutionResult.ok_text("ok")
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        del ctx
        self.calls.append(call)
        return self.result


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("read_file", "Read", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


async def _collect_events(gen) -> list[object]:  # type: ignore[no-untyped-def]
    return [ev async for ev in gen]


class TestRunRealtimeTurn:
    async def test_commits_user_and_assistant_messages(self) -> None:
        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        events = await _collect_events(
            run_realtime_turn(
                "hello",
                "hi there",
                [],
                ctx,
                history=history,
                tool_executor=executor,
            )
        )
        assert any(isinstance(e, Thinking) for e in events)
        assert events
        assert any(isinstance(e, AssistantDelta) for e in events)
        assert any(isinstance(e, TurnComplete) for e in events)
        assert len(history.rows) == 2
        assert history.rows[0].role == "user"
        assert history.rows[1].role == "assistant"
        assert history.rows[1].content[0] == Text(text="hi there")

    async def test_dispatches_tool_and_returns_result(self) -> None:
        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        tool_results: list[ToolResponse] = []
        events = await _collect_events(
            run_realtime_turn(
                "read it",
                "",
                [RealtimeToolCall(call_id="c1", name="read_file", args={"path": "x"})],
                ctx,
                history=history,
                tool_executor=executor,
                tool_results_out=tool_results,
            )
        )
        assert any(isinstance(e, ToolCallStarted) for e in events)
        assert any(isinstance(e, ToolCallResult) for e in events)
        assert len(tool_results) == 1
        assert tool_results[0].tool_name == "read_file"
        assert len(executor.calls) == 1
        assert executor.calls[0].name == "read_file"
        # user + assistant(with tool request) + tool response user message
        assert len(history.rows) == 3
        assert history.rows[1].role == "assistant"
        assert isinstance(history.rows[1].content[0], ToolRequest)
        assert history.rows[2].role == "user"
        assert isinstance(history.rows[2].content[0], ToolResponse)

    async def test_empty_assistant_text_and_no_tools(self) -> None:
        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        _ = await _collect_events(
            run_realtime_turn(
                "hello",
                "",
                [],
                ctx,
                history=history,
                tool_executor=executor,
            )
        )
        assert len(history.rows) == 1
        assert history.rows[0].role == "user"

    async def test_inject_texts_out_collects_pre_tool_hook_text(self) -> None:
        from monkeybot.core.hooks import HookEvent, HookManager, HookPayload

        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        inject_texts: list[str] = []

        async def _inject(payload: HookPayload) -> None:
            if payload.event == HookEvent.PRE_TOOL:
                payload.inject_text = "remember this"

        mgr = HookManager()
        mgr.register(HookEvent.PRE_TOOL, _inject)
        await _collect_events(
            run_realtime_turn(
                "read it",
                "",
                [RealtimeToolCall(call_id="c1", name="read_file", args={"path": "x"})],
                ctx,
                history=history,
                tool_executor=executor,
                hook_manager=mgr,
                inject_texts_out=inject_texts,
            )
        )
        assert inject_texts == ["remember this"]
