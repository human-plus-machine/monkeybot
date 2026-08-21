"""Tests for ``monkeybot.core.runtime.realtime_loop``."""

from __future__ import annotations

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Message, ToolCall
from monkeybot.core.llm.realtime_provider import RealtimeToolCall
from monkeybot.core.runtime.events import (
    AssistantDelta,
    AssistantTextEnded,
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

    async def append(
        self, thread_id: str, message: Message, *, turn_id: str | None = None, message_id: str | None = None
    ) -> None:
        del thread_id, turn_id, message_id
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
        assert AssistantTextEnded(request_id="r1", text="hi there") in events
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

    async def test_parse_error_short_circuits_execution(self) -> None:
        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        tool_results: list[ToolResponse] = []
        events = await _collect_events(
            run_realtime_turn(
                "broken call",
                "",
                [
                    RealtimeToolCall(
                        call_id="c1",
                        name="read_file",
                        args={},
                        parse_error="Failed to parse tool args: not-json",
                    )
                ],
                ctx,
                history=history,
                tool_executor=executor,
                tool_results_out=tool_results,
            )
        )
        assert executor.calls == []
        started = next(e for e in events if isinstance(e, ToolCallStarted))
        assert started.parse_error == "Failed to parse tool args: not-json"
        result = next(e for e in events if isinstance(e, ToolCallResult))
        assert result.error == "Failed to parse tool args: not-json"
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True

    async def test_all_parse_error_batch_rejects_without_executing(self) -> None:
        """Realtime rejects all-parse_error batches (no Done.truncated path)."""
        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        events = await _collect_events(
            run_realtime_turn(
                "broken batch",
                "",
                [
                    RealtimeToolCall(
                        call_id="c1",
                        name="read_file",
                        args={},
                        parse_error="Failed to parse tool args: {",
                    ),
                    RealtimeToolCall(
                        call_id="c2",
                        name="write_file",
                        args={},
                        parse_error="Failed to parse tool args: [",
                    ),
                ],
                ctx,
                history=history,
                tool_executor=executor,
            )
        )
        assert executor.calls == []
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 2
        assert all(r.error is not None for r in results)
        assert results[0].error == "Failed to parse tool args: {"
        assert results[1].error == "Failed to parse tool args: ["

    async def test_skips_empty_user_content(self) -> None:
        history = FakeHistory()
        executor = RecordingExecutor()
        ctx = _ctx()
        await _collect_events(
            run_realtime_turn(
                "",
                "assistant only",
                [],
                ctx,
                history=history,
                tool_executor=executor,
            )
        )
        assert len(history.rows) == 1
        assert history.rows[0].role == "assistant"

    async def test_refreshes_tools_after_successful_enable_mcp(self) -> None:
        from monkeybot.core.runtime.realtime_loop import _REALTIME_MCP_NEW_SESSION_NOTE

        class _Mcp:
            def known_server_names(self) -> list[str]:
                return ["browser"]

            def is_connected(self, name: str) -> bool:
                return name == "browser"

            def all_tools(self) -> list[ToolDef]:
                return [ToolDef("browser__goto", "Go", {})]

        class _Exec(RecordingExecutor):
            def __init__(self) -> None:
                super().__init__(
                    ToolExecutionResult.ok_text(
                        '{"ok": true, "server": "browser", "tools": []}'
                    )
                )
                self.mcp = _Mcp()

        history = FakeHistory()
        executor = _Exec()
        ctx = _ctx()
        inject_texts: list[str] = []
        await _collect_events(
            run_realtime_turn(
                "browse",
                "",
                [RealtimeToolCall(call_id="c1", name="enable_mcp", args={"name": "browser"})],
                ctx,
                history=history,
                tool_executor=executor,
                inject_texts_out=inject_texts,
            )
        )
        assert any(t.name == "browser__goto" for t in ctx.tools)
        assert any(t.name == "list_mcp_resources" for t in ctx.tools)
        assert any(t.name == "read_mcp_resource" for t in ctx.tools)
        assert any(t.name == "list_mcp_prompts" for t in ctx.tools)
        assert any(t.name == "get_mcp_prompt" for t in ctx.tools)
        assert _REALTIME_MCP_NEW_SESSION_NOTE in inject_texts

    async def test_failed_enable_mcp_does_not_refresh_tools(self) -> None:
        class _Mcp:
            def known_server_names(self) -> list[str]:
                return ["browser"]

            def is_connected(self, name: str) -> bool:
                return False

            def all_tools(self) -> list[ToolDef]:
                return [ToolDef("browser__goto", "Go", {})]

        class _Exec(RecordingExecutor):
            def __init__(self) -> None:
                super().__init__(ToolExecutionResult.err("Unknown MCP server 'missing'"))
                self.mcp = _Mcp()

        history = FakeHistory()
        executor = _Exec()
        ctx = _ctx()
        before = list(ctx.tools)
        inject_texts: list[str] = []
        await _collect_events(
            run_realtime_turn(
                "browse",
                "",
                [RealtimeToolCall(call_id="c1", name="enable_mcp", args={"name": "missing"})],
                ctx,
                history=history,
                tool_executor=executor,
                inject_texts_out=inject_texts,
            )
        )
        assert [t.name for t in ctx.tools] == [t.name for t in before]
        assert inject_texts == []

    async def test_tool_elicitation_block_awaits_user_response(self) -> None:
        import asyncio

        from monkeybot.core.runtime.events import ActionRequiredEvent
        from monkeybot.core.types.content_blocks import ActionRequired, ElicitationAction

        class ElicitingExecutor:
            async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
                del call, ctx
                return ToolExecutionResult.ok_blocks(
                    [
                        ActionRequired(
                            data=ElicitationAction(
                                id="el-1",
                                message="What is your name?",
                                requested_schema={"type": "object"},
                            )
                        )
                    ]
                )

        class FakeBus:
            def __init__(self) -> None:
                self._futs: dict[str, asyncio.Future[object]] = {}

            def register_pending(self, pending_key: str) -> asyncio.Future[object]:
                fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
                self._futs[pending_key] = fut
                return fut

            def resolve(self, pending_key: str, payload: dict[str, object]) -> None:
                self._futs[pending_key].set_result(payload)

        history = FakeHistory()
        bus = FakeBus()
        ctx = _ctx()

        async def _drive() -> list[object]:
            gen = run_realtime_turn(
                "hi",
                "calling tool",
                [RealtimeToolCall(call_id="c1", name="ask", args={})],
                ctx,
                history=history,
                tool_executor=ElicitingExecutor(),
                pending_bus=bus,
            )
            events: list[object] = []
            while True:
                try:
                    ev = await gen.__anext__()
                except StopAsyncIteration:
                    break
                events.append(ev)
                if isinstance(ev, ActionRequiredEvent):
                    bus.resolve(
                        ev.id,
                        {"user_data": {"name": "Ada"}, "cancelled": False, "approved": True},
                    )
            return events

        events = await _drive()
        elicit = next(e for e in events if isinstance(e, ActionRequiredEvent))
        assert elicit.id == "el-1"
        assert elicit.action_type == "elicitation"
        result = next(e for e in events if isinstance(e, ToolCallResult))
        assert result.error is None
        assert "Ada" in (result.result or "")

    async def test_tool_confirm_future_cancel_settles(self) -> None:
        import asyncio
        import dataclasses
        from collections import deque
        from typing import Literal

        from monkeybot.core.runtime.events import Error, ToolCallResult, ToolConfirmationRequestEvent
        from monkeybot.core.tools.inspector import Decision
        from monkeybot.core.types.content_blocks import ToolResponse

        class ConfirmInspector:
            async def check(self, call, ctx):  # type: ignore[no-untyped-def]
                del call, ctx
                return Decision(kind="confirm", message="Allow?")

        class CancelOnConfirmBus:
            def __init__(self) -> None:
                self.pending_responses: dict[str, asyncio.Future[object]] = {}
                self.terminated_pending_keys: deque[str] = deque(maxlen=256)

            def register_pending(self, pending_key: str) -> asyncio.Future[object]:
                fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
                self.pending_responses[pending_key] = fut
                return fut

            def is_pending_or_terminal(
                self, key: str
            ) -> Literal["pending", "terminated", "unknown"]:
                if key in self.pending_responses:
                    return "pending"
                if key in self.terminated_pending_keys:
                    return "terminated"
                return "unknown"

            def resolve_pending(self, key: str, payload: object) -> bool:
                fut = self.pending_responses.get(key)
                if fut is None or fut.done():
                    return False
                fut.set_result(payload)
                self.pending_responses.pop(key, None)
                self.terminated_pending_keys.append(key)
                return True

            def abandon_pending_timeout(self, key: str) -> None:
                fut = self.pending_responses.pop(key, None)
                if fut is not None and not fut.done():
                    fut.cancel()
                self.terminated_pending_keys.append(key)

            def abandon_pending_cancel_all(self) -> None:
                for key in list(self.pending_responses.keys()):
                    fut = self.pending_responses.pop(key, None)
                    if fut is not None and not fut.done():
                        fut.cancel()
                    self.terminated_pending_keys.append(key)

        bus = CancelOnConfirmBus()
        history = FakeHistory()
        cancel = asyncio.Event()
        ctx = dataclasses.replace(_ctx(), cancelled=cancel)
        agen = run_realtime_turn(
            "hi",
            "calling",
            [RealtimeToolCall(call_id="c1", name="shell", args={"cmd": "ls"})],
            ctx,
            history=history,
            tool_executor=RecordingExecutor(),
            inspectors=[ConfirmInspector()],
            pending_bus=bus,
        )

        while True:
            ev = await agen.__anext__()
            if isinstance(ev, ToolConfirmationRequestEvent):
                break

        fut = bus.pending_responses.get("c1")
        assert fut is not None and not fut.done()
        # Match ClientInterruptFrame: set cancelled, then abandon pending.
        cancel.set()
        bus.abandon_pending_cancel_all()

        events = [ev]
        async for trailing in agen:
            events.append(trailing)

        assert any(isinstance(e, Error) and "cancel" in e.error.lower() for e in events)
        cancel_results = [
            e for e in events if isinstance(e, ToolCallResult) and e.error
        ]
        assert cancel_results, "confirm-cancel must emit cancel tool results"
        tool_msgs = [
            m
            for m in history.rows
            if m.role == "user" and any(isinstance(b, ToolResponse) for b in m.content)
        ]
        assert tool_msgs, "confirm-cancel must settle tool responses into history"

    async def test_tool_confirm_task_cancel_propagates(self) -> None:
        import asyncio
        from collections import deque
        from typing import Literal

        from monkeybot.core.runtime.events import ToolConfirmationRequestEvent
        from monkeybot.core.tools.inspector import Decision

        class ConfirmInspector:
            async def check(self, call, ctx):  # type: ignore[no-untyped-def]
                del call, ctx
                return Decision(kind="confirm", message="Allow?")

        class WaitOnConfirmBus:
            def __init__(self) -> None:
                self.pending_responses: dict[str, asyncio.Future[object]] = {}
                self.terminated_pending_keys: deque[str] = deque(maxlen=256)
                self.confirm_event = asyncio.Event()

            def register_pending(self, pending_key: str) -> asyncio.Future[object]:
                fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
                self.pending_responses[pending_key] = fut
                self.confirm_event.set()
                return fut

            def is_pending_or_terminal(
                self, key: str
            ) -> Literal["pending", "terminated", "unknown"]:
                if key in self.pending_responses:
                    return "pending"
                if key in self.terminated_pending_keys:
                    return "terminated"
                return "unknown"

            def resolve_pending(self, key: str, payload: object) -> bool:
                fut = self.pending_responses.get(key)
                if fut is None or fut.done():
                    return False
                fut.set_result(payload)
                self.pending_responses.pop(key, None)
                self.terminated_pending_keys.append(key)
                return True

            def abandon_pending_timeout(self, key: str) -> None:
                fut = self.pending_responses.pop(key, None)
                if fut is not None and not fut.done():
                    fut.cancel()
                self.terminated_pending_keys.append(key)

            def abandon_pending_cancel_all(self) -> None:
                for key in list(self.pending_responses.keys()):
                    fut = self.pending_responses.pop(key, None)
                    if fut is not None and not fut.done():
                        fut.cancel()
                    self.terminated_pending_keys.append(key)

        bus = WaitOnConfirmBus()
        history = FakeHistory()
        ctx = _ctx()
        agen = run_realtime_turn(
            "hi",
            "calling",
            [RealtimeToolCall(call_id="c1", name="shell", args={"cmd": "ls"})],
            ctx,
            history=history,
            tool_executor=RecordingExecutor(),
            inspectors=[ConfirmInspector()],
            pending_bus=bus,
        )

        async def _consume() -> None:
            async for _ in agen:
                pass

        task = asyncio.create_task(_consume())
        await bus.confirm_event.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()

    async def test_tool_confirm_teardown_abandon_does_not_settle(self) -> None:
        """Websocket close abandons pending keys without setting cancelled."""
        import asyncio
        import dataclasses
        from collections import deque
        from typing import Literal

        from monkeybot.core.runtime.events import ToolConfirmationRequestEvent
        from monkeybot.core.tools.inspector import Decision

        class ConfirmInspector:
            async def check(self, call, ctx):  # type: ignore[no-untyped-def]
                del call, ctx
                return Decision(kind="confirm", message="Allow?")

        class WaitOnConfirmBus:
            def __init__(self) -> None:
                self.pending_responses: dict[str, asyncio.Future[object]] = {}
                self.terminated_pending_keys: deque[str] = deque(maxlen=256)
                self.confirm_event = asyncio.Event()

            def register_pending(self, pending_key: str) -> asyncio.Future[object]:
                fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
                self.pending_responses[pending_key] = fut
                self.confirm_event.set()
                return fut

            def is_pending_or_terminal(
                self, key: str
            ) -> Literal["pending", "terminated", "unknown"]:
                if key in self.pending_responses:
                    return "pending"
                if key in self.terminated_pending_keys:
                    return "terminated"
                return "unknown"

            def resolve_pending(self, key: str, payload: object) -> bool:
                fut = self.pending_responses.get(key)
                if fut is None or fut.done():
                    return False
                fut.set_result(payload)
                self.pending_responses.pop(key, None)
                self.terminated_pending_keys.append(key)
                return True

            def abandon_pending_timeout(self, key: str) -> None:
                fut = self.pending_responses.pop(key, None)
                if fut is not None and not fut.done():
                    fut.cancel()
                self.terminated_pending_keys.append(key)

            def abandon_pending_cancel_all(self) -> None:
                for key in list(self.pending_responses.keys()):
                    fut = self.pending_responses.pop(key, None)
                    if fut is not None and not fut.done():
                        fut.cancel()
                    self.terminated_pending_keys.append(key)

        bus = WaitOnConfirmBus()
        history = FakeHistory()
        cancel = asyncio.Event()
        ctx = dataclasses.replace(_ctx(), cancelled=cancel)
        agen = run_realtime_turn(
            "hi",
            "calling",
            [RealtimeToolCall(call_id="c1", name="shell", args={"cmd": "ls"})],
            ctx,
            history=history,
            tool_executor=RecordingExecutor(),
            inspectors=[ConfirmInspector()],
            pending_bus=bus,
        )

        async def _consume() -> None:
            async for _ in agen:
                pass

        task = asyncio.create_task(_consume())
        await bus.confirm_event.wait()
        await asyncio.sleep(0.05)
        bus.abandon_pending_cancel_all()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not any(
            isinstance(b, ToolResponse)
            for m in history.rows
            for b in m.content
        )
