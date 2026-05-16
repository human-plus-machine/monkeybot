"""Unit tests for monkeybot.core.runtime.loop."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.context import TurnContext
from monkeybot.core.runtime.events import (
    AssistantDelta,
    ContextSummarized,
    ContextSummarizing,
    Error,
    SystemPromptSnapshot,
    Thinking,
    ToolCallResult,
    TurnComplete,
)
from monkeybot.core.tools.inspector import Decision
from monkeybot.core.runtime.loop import _chunk_tool_calls, run
from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, ToolCall, UsageEvent
from monkeybot.core.types.types_tools import ToolDef


def _flatten_text_from_message(m: Message) -> str:
    return "".join(b.text for b in m.content if isinstance(b, Text))


def _ctx(
    *,
    workspace_root: Path | None = None,
    context_window_tokens: int = 200_000,
) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        workspace_root=workspace_root,
        context_window_tokens=context_window_tokens,
    )


def _ctx_with_task(
    *,
    workspace_root: Path | None = None,
    context_window_tokens: int = 200_000,
) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("task", "Spawn subagent", {}),
            ToolDef("run_command", "Run shell", {}),
        ],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        workspace_root=workspace_root,
        context_window_tokens=context_window_tokens,
    )


class FakeHistory:
    def __init__(self, preload: list[Message] | None = None) -> None:
        self.rows: list[Message] = list(preload) if preload is not None else []
        self.reset_calls: list[tuple[str, list[Message]]] = []

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        del thread_id, limit
        return list(self.rows)

    async def append(self, thread_id: str, message: Message) -> None:
        del thread_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        self.reset_calls.append((thread_id, list(messages)))
        self.rows = list(messages)


class FakeProvider:
    def __init__(self, scripted: list[list[object]]) -> None:
        self._scripted = scripted
        self.stream_calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supports_streaming(self) -> bool:
        return True

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, model
        idx = self.stream_calls
        self.stream_calls += 1
        if idx >= len(self._scripted):
            return
        for ev in self._scripted[idx]:
            yield ev  # type: ignore[misc]


class RecordingExecutor:
    def __init__(self, result: tuple[str | None, str | None] = ("ok", None)) -> None:
        self.result = result
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
        del ctx
        self.calls.append(call)
        return self.result


class DenyingInspector:
    async def check(self, call, ctx):
        del call, ctx
        return Decision(kind="deny", message="tier deny")


class AllowInspector:
    async def check(self, call, ctx):
        del call, ctx
        return Decision(kind="allow")


class OrderRecordingInspector:
    def __init__(self) -> None:
        self.seen_call_ids: list[str] = []

    async def check(self, call, ctx):
        del ctx
        self.seen_call_ids.append(call.call_id)
        return Decision(kind="allow")


class ConfirmInspector:
    async def check(self, call, ctx):
        del call, ctx
        return Decision(kind="confirm", message=None)


class PresetPendingBus:
    """Registers pending futures with optional pre-resolved payloads (Story 5 loop tests)."""

    def __init__(self, preset: dict[str, Any]) -> None:
        self._preset = preset

    def register_pending(self, key: str) -> asyncio.Future[Any]:
        fut = asyncio.get_running_loop().create_future()
        if key in self._preset:
            fut.set_result(self._preset[key])
        return fut


@pytest.mark.asyncio
async def test_run_no_tools_yields_assistant_then_turn_complete() -> None:
    prov = FakeProvider(
        [
            [
                TextDelta(text="hi"),
                UsageEvent(input_tokens=1, output_tokens=2, cached_tokens=3),
                Done(),
            ]
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = _ctx()
    events = []
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=3,
    ):
        events.append(e)
    assert any(isinstance(e, Thinking) for e in events)
    deltas = [e for e in events if isinstance(e, AssistantDelta)]
    assert [e.delta for e in deltas] == ["hi"]
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].usage.input_tokens == 1
    assert events[-1].usage.output_tokens == 2
    assert events[-1].usage.cached_tokens == 3


@pytest.mark.asyncio
async def test_run_usage_defaults_when_missing_usage_events() -> None:
    prov = FakeProvider([[TextDelta(text="x"), Done()]])
    hist = FakeHistory()
    ctx = _ctx()
    events = []
    async for e in run(
        "m",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(e)
    assert not any(isinstance(e, Error) for e in events)
    tc = events[-1]
    assert isinstance(tc, TurnComplete)
    assert tc.usage.input_tokens == 0
    assert tc.usage.output_tokens == 0
    assert tc.usage.cached_tokens == 0
    assert tc.usage.duration_ms == 0


@pytest.mark.asyncio
async def test_run_inspector_deny_then_second_turn_completes() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                Done(),
            ],
            [TextDelta(text="after deny"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[DenyingInspector()],
        tool_executor=exe,
        max_turns=3,
    ):
        events.append(e)
    assert prov.stream_calls == 2
    assert exe.calls == []
    kinds = [type(e).__name__ for e in events]
    assert "ToolCallStarted" in kinds
    assert any(isinstance(e, Error) and "tier deny" in e.error for e in events)
    assert any(isinstance(e, AssistantDelta) and e.delta == "after deny" for e in events)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_inspector_allow_invokes_executor_once() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                Done(),
            ],
            [TextDelta(text="k"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = _ctx()
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=3,
    ):
        if isinstance(e, TurnComplete):
            break
    assert len(exe.calls) == 1
    assert len(hist.rows) >= 2


@pytest.mark.asyncio
async def test_run_cancellation_between_two_tools_second_skipped() -> None:
    cancel = asyncio.Event()

    class CancelAfterFirst(RecordingExecutor):
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
            await super().execute(call=call, ctx=ctx)
            if len(self.calls) == 1:
                cancel.set()
            return self.result

    prov = FakeProvider(
        [
            [
                ToolCall(call_id="a", name="run_command", args={"command": "a"}),
                ToolCall(call_id="b", name="run_command", args={"command": "b"}),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    exe = CancelAfterFirst()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        cancelled=cancel,
        max_turns=3,
    ):
        events.append(e)
    assert [c.call_id for c in exe.calls] == ["a"]
    assert any(isinstance(e, Error) and "cancelled" in e.error.lower() for e in events)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_generator_closes_without_pending_tasks() -> None:
    cancel = asyncio.Event()

    class CancelAfterFirst(RecordingExecutor):
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
            await super().execute(call=call, ctx=ctx)
            if len(self.calls) == 1:
                cancel.set()
            return self.result

    before = set(asyncio.all_tasks())
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="a", name="run_command", args={"command": "a"}),
                ToolCall(call_id="b", name="run_command", args={"command": "b"}),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    exe = CancelAfterFirst()
    ctx = _ctx()
    agen = run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        cancelled=cancel,
        max_turns=3,
    )
    async for _ in agen:
        pass
    await asyncio.sleep(0)
    after = set(asyncio.all_tasks())
    extra = after - before
    assert len(extra) == 0


@pytest.mark.asyncio
async def test_run_empty_model_after_tools_retries_then_succeeds() -> None:
    """Regression: do not end the run after tools when the model streams no text (only Done)."""
    prov = FakeProvider(
        [
            [ToolCall(call_id="c1", name="run_command", args={"command": "x"}), Done()],
            [Done()],
            [TextDelta(text="summary"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=6,
    ):
        events.append(e)
    assert prov.stream_calls == 3
    deltas = [e.delta for e in events if isinstance(e, AssistantDelta)]
    assert deltas == ["summary"]
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_max_turns_emits_error_and_turn_complete() -> None:
    script = [
        ToolCall(call_id="t", name="run_command", args={"command": "echo x"}),
        Done(),
    ]
    prov = FakeProvider([script, script, script])
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=2,
    ):
        events.append(e)
    assert prov.stream_calls == 2
    assert any(isinstance(e, Error) and "Max turns exceeded" in e.error for e in events)
    assert isinstance(events[-1], TurnComplete)


def test_chunk_tool_calls_groups_consecutive_tasks() -> None:
    a = ToolCall(call_id="a", name="task", args={"task": "1"})
    b = ToolCall(call_id="b", name="task", args={"task": "2"})
    c = ToolCall(call_id="c", name="run_command", args={"command": "x"})
    d = ToolCall(call_id="d", name="task", args={"task": "3"})
    chunks = _chunk_tool_calls([a, b, c, d])
    assert len(chunks) == 3
    assert [len(ch) for ch in chunks] == [2, 1, 1]
    assert chunks[0] == [a, b]
    assert chunks[1] == [c]
    assert chunks[2] == [d]


@pytest.mark.asyncio
async def test_parallel_task_tools_cap_concurrent_executions() -> None:
    """Up to 12 ``task`` calls in one batch; at most 10 run inside the semaphore at once."""
    _gate: dict[str, int] = {"n": 0, "max": 0}
    _lock = asyncio.Lock()

    class CountingExecutor:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
            del ctx
            if call.name != "task":
                return ("x", None)
            async with _lock:
                _gate["n"] += 1
                _gate["max"] = max(_gate["max"], _gate["n"])
            await asyncio.sleep(0.04)
            async with _lock:
                _gate["n"] -= 1
            return ('{"ok": true}', None)

    calls = [ToolCall(call_id=f"t{i:02d}", name="task", args={"task": f"j{i}"}) for i in range(12)]
    prov = FakeProvider([[*calls, Done()]])
    hist = FakeHistory()
    exe = CountingExecutor()
    ctx = _ctx_with_task()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=4,
    ):
        pass
    assert _gate["max"] == 10


@pytest.mark.asyncio
async def test_run_provider_raises_wrapped_as_error() -> None:
    class BoomProvider:
        @property
        def name(self) -> str:
            return "boom"

        @property
        def supports_streaming(self) -> bool:
            return True

        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
        ) -> AsyncIterator[ProviderEvent]:
            del messages, tools, model
            raise RuntimeError("boom")
            yield  # pragma: no cover

    hist = FakeHistory()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=BoomProvider(),
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        events.append(e)
    assert isinstance(events[0], Thinking)
    assert isinstance(events[1], SystemPromptSnapshot)
    assert isinstance(events[2], Error)
    assert "boom" in events[2].error
    assert isinstance(events[3], TurnComplete)


@pytest.mark.asyncio
async def test_run_cleanup_spill_at_start(tmp_path: Path) -> None:
    spill = tmp_path / ".monkeybot" / "spill" / "t1"
    spill.mkdir(parents=True)
    (spill / "prev.txt").write_text("stale", encoding="utf-8")
    prov = FakeProvider([[TextDelta(text="ok"), Done()]])
    hist = FakeHistory()
    ctx = _ctx(workspace_root=tmp_path)
    async for _ in run(
        "hi",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass
    assert not spill.exists()


@pytest.mark.asyncio
async def test_run_emits_context_summarize_events_when_over_cap(tmp_path: Path) -> None:
    preload = [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text="z" * 600)],
        )
        for i in range(8)
    ]
    hist = FakeHistory(preload)
    prov = FakeProvider(
        [
            [TextDelta(text=" compressed summary "), Done()],
            [TextDelta(text="final"), Done()],
        ]
    )
    ctx = _ctx(workspace_root=tmp_path, context_window_tokens=800)
    events = []
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        events.append(e)
    kinds = [type(x).__name__ for x in events]
    assert "ContextSummarizing" in kinds
    assert "ContextSummarized" in kinds
    assert prov.stream_calls == 2
    assert len(hist.reset_calls) == 1
    assert any(
        isinstance(x, Text) and "[Context Summary]" in x.text
        for m in hist.rows
        for x in m.content
    )


@pytest.mark.asyncio
async def test_run_no_context_summarize_events_when_under_cap(tmp_path: Path) -> None:
    preload = [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text="z" * 600)],
        )
        for i in range(8)
    ]
    hist = FakeHistory(preload)
    prov = FakeProvider([[TextDelta(text="final"), Done()]])
    ctx = _ctx(workspace_root=tmp_path, context_window_tokens=2_000_000)
    events = []
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        events.append(e)
    kinds = [type(x).__name__ for x in events]
    assert "ContextSummarizing" not in kinds
    assert "ContextSummarized" not in kinds
    assert prov.stream_calls == 1
    assert hist.reset_calls == []


@pytest.mark.asyncio
async def test_loop_picks_up_refreshed_memory_between_turns(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("initial line\n", encoding="utf-8")

    class CaptureFakeProvider:
        def __init__(self, scripted: list[list[object]]) -> None:
            self._scripted = scripted
            self.stream_calls = 0
            self.captured_messages: list[list[Message]] = []

        @property
        def name(self) -> str:
            return "fake"

        @property
        def supports_streaming(self) -> bool:
            return True

        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
        ) -> AsyncIterator[ProviderEvent]:
            self.captured_messages.append(list(messages))
            del tools, model
            idx = self.stream_calls
            self.stream_calls += 1
            if idx >= len(self._scripted):
                return
            for ev in self._scripted[idx]:
                yield ev  # type: ignore[misc]

    class BumpIndexExecutor(RecordingExecutor):
        def __init__(self, memory_dir: Path) -> None:
            super().__init__()
            self._memory_dir = memory_dir

        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
            del call, ctx
            (self._memory_dir / "INDEX.md").write_text(
                "initial line\nnew memory from tool\n",
                encoding="utf-8",
            )
            return ("ok", None)

    prov = CaptureFakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "noop"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = BumpIndexExecutor(mem)
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=["initial line"],
        skills=[],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        memory_path=mem,
    )
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=5,
    ):
        if isinstance(e, TurnComplete):
            break

    assert len(prov.captured_messages) == 2
    sys1 = _flatten_text_from_message(prov.captured_messages[0][0])
    sys2 = _flatten_text_from_message(prov.captured_messages[1][0])
    assert "initial line" in sys1
    assert "new memory from tool" not in sys1
    assert "new memory from tool" in sys2


@pytest.mark.asyncio
async def test_parallel_task_results_appended_in_call_id_order() -> None:
    """Parallel task calls must append to history in call_id order regardless of completion order.

    The executor finishes b1 first, then a1, then c1 (reverse alphabetical).
    History and ToolCallResult events must still appear in sorted call_id order: a1, b1, c1.
    This guards against regressions where appends happen inside the async task (completion order)
    instead of after asyncio.gather (submission/call_id order).
    """
    # Reverse-alphabetical finish order: b1 (0.01s) → a1 (0.02s) → c1 (0.05s)
    delays = {"c1": 0.05, "a1": 0.02, "b1": 0.01}

    class SlowExecutor:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
            del ctx
            await asyncio.sleep(delays.get(call.call_id, 0.01))
            return (f"result:{call.call_id}", None)

    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="task", args={"task": "C"}),
                ToolCall(call_id="a1", name="task", args={"task": "A"}),
                ToolCall(call_id="b1", name="task", args={"task": "B"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    hist = FakeHistory()
    ctx = _ctx_with_task()
    events: list[object] = []
    async for e in run(
        "go",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=SlowExecutor(),
        max_turns=4,
    ):
        events.append(e)

    # All three parallel task responses must be batched into a single user
    # Message so that Gemini sees one functionResponse turn matching one
    # functionCall turn (enforced by 400 INVALID_ARGUMENT otherwise).
    tool_resp_messages = [
        m
        for m in hist.rows
        if m.role == "user"
        and m.content
        and isinstance(m.content[0], ToolResponse)
    ]
    assert len(tool_resp_messages) == 1, (
        "parallel task responses must be consolidated into one user Message"
    )
    consolidated = tool_resp_messages[0].content
    assert [b.id for b in consolidated] == ["a1", "b1", "c1"], (
        "tool result blocks must be in call_id order within the consolidated message"
    )

    result_events = [e for e in events if isinstance(e, ToolCallResult)]
    assert [e.tool for e in result_events] == ["task", "task", "task"]
    # Results must contain the correct payloads tied to call_ids
    result_bodies = [e.result for e in result_events]
    assert result_bodies == ["result:a1", "result:b1", "result:c1"], (
        "ToolCallResult events must be emitted in call_id order"
    )


@pytest.mark.asyncio
async def test_loop_tool_only_assistant_history_no_placeholder_json() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [TextDelta(text="x"), Done()],
        ]
    )
    hist = FakeHistory()
    ctx = _ctx()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    tool_only_assistant = next(
        m
        for m in hist.rows
        if m.role == "assistant"
        and len(m.content) == 1
        and isinstance(m.content[0], ToolRequest)
    )
    assert not any(isinstance(b, Text) for b in tool_only_assistant.content)
    serialized = json.dumps([b.to_dict() for b in tool_only_assistant.content])
    assert 'tool_calls"' not in serialized


@pytest.mark.asyncio
async def test_loop_mixed_text_and_tools_assistant_order() -> None:
    prov = FakeProvider(
        [
            [
                TextDelta(text="ok"),
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [TextDelta(text="tail"), Done()],
        ]
    )
    hist = FakeHistory()
    ctx = _ctx()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    mixed = next(
        m
        for m in hist.rows
        if m.role == "assistant"
        and len(m.content) >= 2
        and isinstance(m.content[0], Text)
        and isinstance(m.content[1], ToolRequest)
    )
    assert mixed.content[0].text == "ok"
    assert mixed.content[1].id == "c1"


@pytest.mark.asyncio
async def test_loop_tool_success_user_row_shape() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="x1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [TextDelta(text="k"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor(("tool-output-ok", None))
    ctx = _ctx()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=4,
    ):
        pass
    block = next(
        b
        for m in hist.rows
        for b in m.content
        if isinstance(b, ToolResponse) and b.id == "x1"
    )
    assert block.tool_name == "run_command"
    assert block.is_error is False
    assert len(block.result) == 1
    assert isinstance(block.result[0], Text)
    assert block.result[0].text == "tool-output-ok"


@pytest.mark.asyncio
async def test_loop_tool_error_user_row_shape() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="e1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [TextDelta(text="k"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor((None, "executor err line"))
    ctx = _ctx()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=4,
    ):
        pass
    block = next(
        b
        for m in hist.rows
        for b in m.content
        if isinstance(b, ToolResponse) and b.id == "e1"
    )
    assert block.is_error is True
    assert len(block.result) == 1
    assert isinstance(block.result[0], Text)
    assert block.result[0].text == "executor err line"


@pytest.mark.asyncio
async def test_loop_inspector_deny_user_row_shape() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="d1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [TextDelta(text="after"), Done()],
        ]
    )
    hist = FakeHistory()
    ctx = _ctx()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[DenyingInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    block = next(
        b
        for m in hist.rows
        for b in m.content
        if isinstance(b, ToolResponse) and b.id == "d1"
    )
    assert block.is_error is True
    assert len(block.result) == 1
    assert isinstance(block.result[0], Text)
    assert "deny" in block.result[0].text.lower()


@pytest.mark.asyncio
async def test_loop_follow_up_after_tool_uses_tool_response_detection() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [Done()],
            [TextDelta(text="summary"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=6,
    ):
        events.append(e)
    assert prov.stream_calls == 3
    deltas = [e.delta for e in events if isinstance(e, AssistantDelta)]
    assert deltas == ["summary"]
    assert any(
        m.role == "user" and any(isinstance(b, ToolResponse) for b in m.content)
        for m in hist.rows
    )


@pytest.mark.asyncio
async def test_system_prompt_snapshot_still_plain_string_event() -> None:
    prov = FakeProvider([[TextDelta(text="hi"), Done()]])
    hist = FakeHistory()
    ctx = _ctx()
    events = []
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(e)
    snaps = [e for e in events if isinstance(e, SystemPromptSnapshot)]
    assert snaps
    assert isinstance(snaps[0].text, str)
    assert len(snaps[0].text) > 0


@pytest.mark.asyncio
async def test_inspector_dispatches_per_tool_request_block_order() -> None:
    insp = OrderRecordingInspector()
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="z9",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                ToolCall(
                    call_id="a1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                ToolCall(
                    call_id="m2",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
            [TextDelta(text="tail"), Done()],
        ]
    )
    hist = FakeHistory()
    ctx = _ctx()
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[insp],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    assert insp.seen_call_ids == ["a1", "m2", "z9"]


@pytest.mark.asyncio
async def test_loop_confirm_decision_raises_if_produced() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={"command": "python skills/foo/run.py"},
                ),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[ConfirmInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(e)
    err_blob = " ".join(e.error for e in events if isinstance(e, Error)).lower()
    assert "confirm" in err_blob or "reserved" in err_blob


@pytest.mark.asyncio
async def test_loop_confirm_approve_executes_executor() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={"command": "true"},
                ),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor()
    ctx = dataclasses.replace(
        _ctx(),
        sse_bus=PresetPendingBus({"c1": {"approved": True}}),
    )
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[ConfirmInspector()],
        tool_executor=exe,
        max_turns=3,
    ):
        pass
    assert exe.calls
    assert any(
        isinstance(b, ToolResponse) and not b.is_error
        for row in hist.rows
        if row.role == "user"
        for b in row.content
    )


@pytest.mark.asyncio
async def test_loop_confirm_deny_appends_error_response() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={}),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    ctx = dataclasses.replace(
        _ctx(),
        sse_bus=PresetPendingBus({"c1": {"approved": False, "reason": "no"}}),
    )
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[ConfirmInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass
    assert any(
        isinstance(b, ToolResponse)
        and b.is_error
        and any(isinstance(x, Text) and x.text == "no" for x in b.result)
        for row in hist.rows
        if row.role == "user"
        for b in row.content
    )


@pytest.mark.asyncio
async def test_loop_confirm_timeout_synthetic_response() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={}),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    ctx = dataclasses.replace(
        _ctx(),
        sse_bus=PresetPendingBus({"c1": {"_timeout": True}}),
    )
    async for _ in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[ConfirmInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass
    assert any(
        isinstance(b, ToolResponse)
        and b.is_error
        and any(isinstance(x, Text) and "did not respond" in x.text for x in b.result)
        for row in hist.rows
        if row.role == "user"
        for b in row.content
    )
