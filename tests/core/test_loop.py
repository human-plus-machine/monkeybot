"""Unit tests for monkeybot.core.runtime.loop."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

import monkeybot.core.runtime.loop as loop_mod
from monkeybot.core.attachments.catalog import AttachmentRecord, SessionAttachmentCatalog
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.llm.provider import (
    Done,
    GroundingEvent as ProviderGroundingEvent,
    Message,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.llm.usage import Usage
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.events import (
    AssistantDelta,
    Error,
    GroundingEvent,
    ImageBlock,
    SystemPromptSnapshot,
    Thinking,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
)
from monkeybot.core.runtime.loop import (
    _compact_history_if_needed,
    _image_events,
    _merge_usage_event,
    _messages_for_provider,
    _usage_to_totals,
    run,
)
from monkeybot.core.runtime.tool_batch import _chunk_tool_calls
from monkeybot.core.testing.mocks_provider import fake_provider_prompt_tokens
from monkeybot.core.tools.inspector import Decision
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import Image, Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.workspace import create_workspace_storage


def _flatten_text_from_message(m: Message) -> str:
    return "".join(b.text for b in m.content if isinstance(b, Text))


def _ctx(
    *,
    workspace_root: Path | None = None,
    context_window_tokens: int = 200_000,
    model: str = "gemini-2.5-flash",
    summarization_model: str | None = None,
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
        model=model,
        summarization_model=summarization_model,
        workspace_root=workspace_root,
        context_window_tokens=context_window_tokens,
    )


def test_run_signature_has_no_unused_run_id() -> None:
    assert "run_id" not in inspect.signature(run).parameters


@pytest.mark.asyncio
async def test_run_logs_uncancel_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenTask:
        def uncancel(self) -> None:
            raise RuntimeError("boom")

    async def raise_cancelled(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        raise asyncio.CancelledError
        yield

    monkeypatch.setattr(loop_mod, "_run_inner", raise_cancelled)
    monkeypatch.setattr(asyncio, "current_task", lambda: BrokenTask())

    provider = FakeProvider([[Done()]])
    events = []
    with caplog.at_level(logging.DEBUG, logger="monkeybot.core.runtime.loop"):
        async for evt in run(
            "hi",
            _ctx(),
            provider=provider,
            history=FakeHistory(),
            inspectors=[],
            tool_executor=RecordingExecutor(),
        ):
            events.append(evt)

    assert any(isinstance(evt, Error) and evt.error == "Request cancelled" for evt in events)
    assert "uncancel cleanup failed" in caplog.text


def _ctx_with_task(
    *,
    workspace_root: Path | None = None,
    context_window_tokens: int = 200_000,
    model: str = "gemini-2.5-flash",
    summarization_model: str | None = None,
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
        model=model,
        summarization_model=summarization_model,
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
    def __init__(self, scripted: list[list[object]], *, name: str = "fake") -> None:
        self._scripted = scripted
        self._name = name
        self.stream_calls = 0
        self.stream_models: list[str] = []
        self.stream_messages: list[list[Message]] = []
        self.count_thinking_budgets: list[int | None] = []
        self.vertex_google_search_calls: list[bool] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def supports_streaming(self) -> bool:
        return True

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        del tools, thinking_budget
        self.stream_messages.append(list(messages))
        self.stream_models.append(model)
        self.vertex_google_search_calls.append(vertex_google_search)
        idx = self.stream_calls
        self.stream_calls += 1
        if idx >= len(self._scripted):
            return
        for ev in self._scripted[idx]:
            yield ev  # type: ignore[misc]

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> int:
        del model, vertex_google_search
        self.count_thinking_budgets.append(thinking_budget)
        return fake_provider_prompt_tokens(messages, tools)


class CountingFakeProvider(FakeProvider):
    """FakeProvider that records messages passed to count_input_tokens."""

    def __init__(self, scripted: list[list[object]]) -> None:
        super().__init__(scripted)
        self.count_snapshots: list[list[Message]] = []

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> int:
        self.count_snapshots.append(list(messages))
        return await super().count_input_tokens(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        )


def _tool_result(
    result: ToolExecutionResult | tuple[str | None, str | None],
) -> ToolExecutionResult:
    if isinstance(result, ToolExecutionResult):
        return result
    text, err = result
    if err is not None:
        return ToolExecutionResult.err(err)
    return ToolExecutionResult.ok_text(text or "")


class RecordingExecutor:
    def __init__(
        self,
        result: ToolExecutionResult | tuple[str | None, str | None] = ("ok", None),
    ) -> None:
        self.result = _tool_result(result)
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
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
async def test_run_streams_thinking_block_deltas_before_assistant() -> None:
    prov = FakeProvider(
        [
            [
                ThinkingDelta(text="consider greeting"),
                ThinkingDelta(text=" then reply", signature="sig-1"),
                TextDelta(text="Hello!"),
                UsageEvent(input_tokens=1, output_tokens=2),
                Done(),
            ]
        ]
    )
    hist = FakeHistory()
    events = []
    async for e in run(
        "hello",
        _ctx(),
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(e)

    thinking_deltas = [e for e in events if isinstance(e, ThinkingBlockDelta)]
    assert [e.text for e in thinking_deltas] == ["consider greeting", " then reply"]
    assert thinking_deltas[-1].signature == "sig-1"

    complete = next(e for e in events if isinstance(e, ThinkingBlockComplete))
    assert complete.signature == "sig-1"

    assistant = [e for e in events if isinstance(e, AssistantDelta)]
    assert [e.delta for e in assistant] == ["Hello!"]
    # Thinking block must close before the first assistant text delta.
    complete_idx = events.index(complete)
    assistant_idx = events.index(assistant[0])
    assert complete_idx < assistant_idx


@pytest.mark.asyncio
async def test_run_closes_thinking_block_when_tools_follow() -> None:
    prov = FakeProvider(
        [
            [
                ThinkingDelta(text="need a tool"),
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [
                TextDelta(text="done"),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
        ]
    )
    hist = FakeHistory()
    events = []
    async for e in run(
        "hello",
        _ctx(),
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=5,
    ):
        events.append(e)

    assert any(isinstance(e, ThinkingBlockDelta) and e.text == "need a tool" for e in events)
    complete = next(e for e in events if isinstance(e, ThinkingBlockComplete))
    tool_started = next(e for e in events if isinstance(e, ToolCallStarted))
    assert events.index(complete) < events.index(tool_started)

def test_merge_usage_event_accumulates_cache_fields() -> None:
    usage = Usage()
    ev = UsageEvent(
        input_tokens=0,
        output_tokens=0,
        cached_tokens=14,
        cache_read_tokens=10,
        cache_creation_tokens=4,
    )
    _merge_usage_event(usage, ev)
    assert usage.cache_read_tokens == 10
    assert usage.cache_creation_tokens == 4
    assert usage.cached_tokens == 14


def test_usage_to_totals_carries_cache_fields() -> None:
    usage = Usage(cache_read_tokens=10, cache_creation_tokens=4)
    totals = _usage_to_totals(usage)
    assert totals.cache_read_tokens == 10
    assert totals.cache_creation_tokens == 4


@pytest.mark.asyncio
async def test_run_computes_cost_for_priced_model() -> None:
    prov = FakeProvider(
        [
            [
                UsageEvent(input_tokens=1_000_000, output_tokens=0, cached_tokens=0),
                Done(),
            ]
        ]
    )
    hist = FakeHistory()
    ctx = _ctx(model="gpt-5")
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
    tc = events[-1]
    assert isinstance(tc, TurnComplete)
    assert tc.usage.cost_usd == pytest.approx(1.25)
    assert tc.usage.cost_usd > 0


@pytest.mark.asyncio
async def test_run_cost_zero_for_unknown_model() -> None:
    prov = FakeProvider(
        [
            [
                UsageEvent(input_tokens=1000, output_tokens=500, cached_tokens=0),
                Done(),
            ]
        ]
    )
    hist = FakeHistory()
    ctx = _ctx(model="totally-unknown-model")
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
    tc = events[-1]
    assert isinstance(tc, TurnComplete)
    assert tc.usage.input_tokens == 1000
    assert tc.usage.output_tokens == 500
    assert tc.usage.cost_usd == 0.0


@pytest.mark.asyncio
async def test_run_turn_complete_carries_cache_split() -> None:
    prov = FakeProvider(
        [
            [
                UsageEvent(
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=14,
                    cache_read_tokens=10,
                    cache_creation_tokens=4,
                ),
                Done(),
            ]
        ]
    )
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
    tc = events[-1]
    assert isinstance(tc, TurnComplete)
    assert tc.usage.cache_read_tokens == 10
    assert tc.usage.cache_creation_tokens == 4


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
    assert tc.usage.duration_ms >= 0


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
    assert "ToolCallResult" in kinds
    assert not any(isinstance(e, Error) for e in events)
    assert any(
        isinstance(e, ToolCallResult)
        and e.tool == "run_command"
        and isinstance(e.error, str)
        and "tier deny" in e.error
        for e in events
    )
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
async def test_run_counts_routine_resume_with_stream_thinking_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GeminiFakeProvider(FakeProvider):
        @property
        def name(self) -> str:
            return "gemini"

    monkeypatch.setenv("MONKEYBOT_RESUME_THINKING_BUDGET", "0")
    prov = GeminiFakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    async for _ in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass

    assert prov.stream_calls == 2
    assert prov.count_thinking_budgets[-1] == 0


@pytest.mark.asyncio
async def test_run_parse_error_short_circuits_execution() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={},
                    parse_error="malformed tool args JSON: boom",
                ),
                Done(),
            ],
            [TextDelta(text="recovered"), Done()],
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
        max_turns=3,
    ):
        events.append(e)
    # The tool must NOT execute with the empty args the provider fell back to.
    assert exe.calls == []
    assert prov.stream_calls == 2
    assert any(
        isinstance(e, ToolCallResult)
        and e.tool == "run_command"
        and isinstance(e.error, str)
        and "malformed tool args JSON" in e.error
        for e in events
    )
    assert any(isinstance(e, AssistantDelta) and e.delta == "recovered" for e in events)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_cancellation_between_two_tools_second_skipped() -> None:
    cancel = asyncio.Event()

    class CancelAfterFirst(RecordingExecutor):
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
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
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
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
    from monkeybot.core.runtime.events import Error
    from monkeybot.core.runtime.loop import _EMPTY_COMPLETION_RECOVERY_NOTE

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
    # A successful post-tool recovery must not surface an Error.
    assert not any(isinstance(e, Error) for e in events)
    # Recovery note injected on the provider call after the empty post-tool turn.
    followup_msgs = prov.stream_messages[2]
    system_text = "".join(
        b.text for m in followup_msgs if m.role == "system" for b in m.content if hasattr(b, "text")
    )
    assert _EMPTY_COMPLETION_RECOVERY_NOTE in system_text


@pytest.mark.asyncio
async def test_run_tool_image_result_yields_image_block() -> None:
    b64 = "iVBORw0KGgo="
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="load_file", args={"path": "./x.png"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor(
        ToolExecutionResult.ok_blocks(
            [Image(mime_type="image/png", data=b64, metadata={"filename": "x.png"})]
        )
    )
    ctx = _ctx()
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=4,
    ):
        events.append(e)
    image_blocks = [e for e in events if isinstance(e, ImageBlock)]
    assert len(image_blocks) == 1
    assert image_blocks[0].mime_type == "image/png"
    assert image_blocks[0].data == b64
    assert image_blocks[0].image_id == "c1:0"
    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert any("[loaded image" in tr.result for tr in tool_results)


def test_image_events_assign_unique_image_ids() -> None:
    result = ToolExecutionResult.ok_blocks(
        [
            Image(mime_type="image/png", data="a"),
            Image(mime_type="image/jpeg", data="b"),
        ]
    )
    events = _image_events("req-1", "call-9", result)
    assert len(events) == 2
    assert events[0].image_id == "call-9:0"
    assert events[1].image_id == "call-9:1"
    assert events[0].request_id == events[1].request_id == "req-1"


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


def test_chunk_tool_calls_groups_consecutive_parallel_safe() -> None:
    a = ToolCall(call_id="a", name="read_file", args={"path": "a"})
    b = ToolCall(call_id="b", name="glob", args={"pattern": "*"})
    c = ToolCall(call_id="c", name="write_file", args={"path": "x", "content": ""})
    d = ToolCall(call_id="d", name="search_memory", args={"query": "q"})
    safe = frozenset({"read_file", "glob", "search_memory"})
    chunks = _chunk_tool_calls([a, b, c, d], parallel_safe=safe)
    assert [len(ch) for ch in chunks] == [2, 1, 1]
    assert [x.name for x in chunks[0]] == ["read_file", "glob"]
    assert chunks[1][0].name == "write_file"
    assert chunks[2][0].name == "search_memory"


@pytest.mark.asyncio
async def test_parallel_safe_tools_results_in_call_id_order() -> None:
    """Parallel-safe read tools run concurrently; results persist in call_id order."""
    delays = {"c1": 0.04, "a1": 0.01, "b1": 0.02}
    concurrent = {"n": 0, "max": 0}
    lock = asyncio.Lock()

    class SlowReadExecutor:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del ctx
            async with lock:
                concurrent["n"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["n"])
            await asyncio.sleep(delays.get(call.call_id, 0.01))
            async with lock:
                concurrent["n"] -= 1
            return ToolExecutionResult.ok_text(f"result:{call.call_id}")

    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="read_file", args={"path": "c"}),
                ToolCall(call_id="a1", name="glob", args={"pattern": "*"}),
                ToolCall(call_id="b1", name="search_memory", args={"query": "q"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    hist = FakeHistory()
    tools = [
        ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True),
        ToolDef("glob", "g", {"type": "object"}, parallel_safe=True),
        ToolDef("search_memory", "s", {"type": "object"}, parallel_safe=True),
    ]
    ctx = TurnContext(**{**_ctx().__dict__, "tools": tools})
    events: list[object] = []
    async for e in run(
        "go",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=SlowReadExecutor(),
        max_turns=4,
    ):
        events.append(e)

    assert concurrent["max"] >= 2
    tool_resp_messages = [
        m
        for m in hist.rows
        if m.role == "user" and m.content and isinstance(m.content[0], ToolResponse)
    ]
    assert len(tool_resp_messages) == 1
    assert [b.id for b in tool_resp_messages[0].content] == ["a1", "b1", "c1"]

    result_events = [e for e in events if isinstance(e, ToolCallResult)]
    assert [e.result for e in result_events] == ["result:a1", "result:b1", "result:c1"]


@pytest.mark.asyncio
async def test_parallel_task_tools_cap_concurrent_executions() -> None:
    """Up to 12 ``task`` calls in one batch; at most 10 run inside the semaphore at once."""
    _gate: dict[str, int] = {"n": 0, "max": 0}
    _lock = asyncio.Lock()

    class CountingExecutor:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del ctx
            if call.name != "task":
                return ToolExecutionResult.ok_text("x")
            async with _lock:
                _gate["n"] += 1
                _gate["max"] = max(_gate["max"], _gate["n"])
            await asyncio.sleep(0.04)
            async with _lock:
                _gate["n"] -= 1
            return ToolExecutionResult.ok_text('{"ok": true}')

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
            thinking_budget: int | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            del messages, tools, model, thinking_budget
            raise RuntimeError("boom")
            yield  # pragma: no cover

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> int:
            del messages, tools, model, thinking_budget
            return 0

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
    assert any(isinstance(e, SystemPromptSnapshot) for e in events)
    err = next(e for e in events if isinstance(e, Error))
    assert "boom" in err.error
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_provider_raises_logs_exception(caplog: pytest.LogCaptureFixture) -> None:
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
            thinking_budget: int | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            del messages, tools, model, thinking_budget
            raise RuntimeError("boom")
            yield  # pragma: no cover

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> int:
            del messages, tools, model, thinking_budget
            return 0

    hist = FakeHistory()
    ctx = _ctx()
    with caplog.at_level(logging.ERROR):
        async for _ in run(
            "u",
            ctx,
            provider=BoomProvider(),
            history=hist,
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=2,
        ):
            pass
    assert any("provider stream failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_run_preserves_spill_across_turns(tmp_path: Path) -> None:
    """Spill files must survive turn starts so later turns can read inventory pointers."""
    spill = tmp_path / ".monkeybot" / "spill" / "t1"
    spill.mkdir(parents=True)
    (spill / "prev.txt").write_text("keep", encoding="utf-8")
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
    assert (spill / "prev.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_run_emits_context_summarize_events_when_over_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONTEXT_SUMMARIZATION_MODEL", raising=False)
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
    assert prov.stream_models == ["gemini-2.5-flash", "gemini-2.5-flash"]
    assert len(hist.reset_calls) == 1
    summarizer_msgs = prov.stream_messages[0]
    assert summarizer_msgs[0].role == "system"
    assert isinstance(summarizer_msgs[0].content[0], Text)
    assert "## Objective" in summarizer_msgs[0].content[0].text
    assert "## Relevant Files" in summarizer_msgs[0].content[0].text
    assert any(
        isinstance(x, Text) and x.text.startswith("[Context Summary]:\n")
        for m in hist.rows
        for x in m.content
    )


@pytest.mark.asyncio
async def test_summarization_call_never_receives_vertex_google_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History summarization is an internal harness call, not an agent turn.

    Regression guard: even with ``vertex_google_search=True`` on the main run,
    the summarization LLM call (``loop.py`` calling ``provider.stream`` directly
    for the summarizer model) must never receive the flag.
    """
    monkeypatch.setenv("CONTEXT_SUMMARIZATION_MODEL", "summarizer-model-x")
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
        ],
        name="gemini",
    )
    ctx = _ctx(workspace_root=tmp_path, context_window_tokens=800)
    async for _ in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
        vertex_google_search=True,
    ):
        pass
    assert prov.stream_models == ["summarizer-model-x", "gemini-2.5-flash"]
    summarizer_call, main_turn_call = prov.vertex_google_search_calls
    assert summarizer_call is False
    assert main_turn_call is True


@pytest.mark.asyncio
async def test_run_summarization_uses_context_summarization_model_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARIZATION_MODEL", "summarizer-model-x")
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
    async for _ in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    assert prov.stream_models == ["summarizer-model-x", "gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_run_summarization_uses_turn_context_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONTEXT_SUMMARIZATION_MODEL", raising=False)
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
    ctx = _ctx(
        workspace_root=tmp_path,
        context_window_tokens=800,
        summarization_model="ctx-summ-model",
    )
    async for _ in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    assert prov.stream_models == ["ctx-summ-model", "gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_run_summarization_env_overrides_turn_context_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARIZATION_MODEL", "env-wins")
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
    ctx = _ctx(
        workspace_root=tmp_path,
        context_window_tokens=800,
        summarization_model="should-not-use",
    )
    async for _ in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    assert prov.stream_models == ["env-wins", "gemini-2.5-flash"]


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


def test_messages_for_provider_no_update_is_passthrough() -> None:
    system = Message(role="system", content=[Text(text="SYS")])
    history = [Message(role="user", content=[Text(text="hi")])]
    out = _messages_for_provider(system, history)
    assert [m.role for m in out] == ["system", "user"]


def test_messages_for_provider_folds_update_into_trailing_user_message() -> None:
    """History ending in "user" (e.g. a tool-response row) must not gain a second

    trailing "user" row — Anthropic/Gemini both reject consecutive same-role
    messages.
    """
    system = Message(role="system", content=[Text(text="SYS")])
    tool_response_row = Message(
        role="user",
        content=[
            ToolResponse(id="c1", tool_name="run_command", result=[Text(text="ok")], is_error=False)
        ],
    )
    history = [
        Message(role="user", content=[Text(text="hi")]),
        Message(role="assistant", content=[ToolRequest(id="c1", name="run_command", args={})]),
        tool_response_row,
    ]
    out = _messages_for_provider(system, history, mid_conversation_update="## System context update\n...")
    roles = [m.role for m in out]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles
    assert len(out) == len(history) + 1  # system + history rows, no extra message
    last = out[-1]
    assert last.role == "user"
    assert any(isinstance(b, ToolResponse) for b in last.content)
    assert any(isinstance(b, Text) and "System context update" in b.text for b in last.content)


def test_messages_for_provider_appends_update_after_trailing_assistant_message() -> None:
    system = Message(role="system", content=[Text(text="SYS")])
    history = [
        Message(role="user", content=[Text(text="hi")]),
        Message(role="assistant", content=[Text(text="ok, thinking...")]),
    ]
    out = _messages_for_provider(system, history, mid_conversation_update="## System context update\n...")
    roles = [m.role for m in out]
    assert roles == ["system", "user", "assistant", "user"]
    assert all(a != b for a, b in zip(roles, roles[1:]))


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
            thinking_budget: int | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            self.captured_messages.append(list(messages))
            del tools, model, thinking_budget
            idx = self.stream_calls
            self.stream_calls += 1
            if idx >= len(self._scripted):
                return
            for ev in self._scripted[idx]:
                yield ev  # type: ignore[misc]

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> int:
            del model, thinking_budget
            return fake_provider_prompt_tokens(messages, tools)

    class BumpIndexExecutor(RecordingExecutor):
        def __init__(self, memory_dir: Path) -> None:
            super().__init__()
            self._memory_dir = memory_dir

        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del call, ctx
            (self._memory_dir / "INDEX.md").write_text(
                "initial line\nnew memory from tool\n",
                encoding="utf-8",
            )
            return ToolExecutionResult.ok_text("ok")

    prov = CaptureFakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "noop"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    uri = "local://" + str(mem.resolve())
    mem_sys = MemorySubsystem(
        storage=create_workspace_storage(uri),
        provider=prov,
        model="gemini-2.5-flash",
        memory_uri=uri,
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
        memory=mem_sys,
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
    turn2 = prov.captured_messages[1]
    turn2_texts = [_flatten_text_from_message(m) for m in turn2]
    all2 = "\n".join(turn2_texts)
    assert "initial line" in sys1
    assert "new memory from tool" not in sys1
    # Volatile memory refresh arrives as a mid-conversation system-context update
    # (leading epoch baseline stays cache-stable).
    assert "new memory from tool" in all2
    # Regression guard: the mid-conversation update must never create two
    # consecutive same-role messages (Anthropic/Gemini both reject this shape).
    roles = [m.role for m in turn2]
    assert all(a != b for a, b in zip(roles, roles[1:])), (
        f"consecutive same-role messages in provider payload: {roles}"
    )
    # History ends in a tool-response "user" row; the update must be folded
    # into that same message, not appended as a second trailing "user" row.
    last = turn2[-1]
    assert last.role == "user"
    assert any(isinstance(b, ToolResponse) for b in last.content)
    assert any(
        isinstance(b, Text) and "new memory from tool" in b.text for b in last.content
    )


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
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del ctx
            await asyncio.sleep(delays.get(call.call_id, 0.01))
            return ToolExecutionResult.ok_text(f"result:{call.call_id}")

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
async def test_serial_mixed_tool_results_consolidated_into_one_user_message() -> None:
    """Serial non-task tools (separate chunks) must share one user Message for Gemini."""
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="b", name="run_command", args={"command": "echo b"}),
                ToolCall(call_id="a", name="read_file", args={"path": "x.md"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    hist = FakeHistory()
    exe = RecordingExecutor(
        ToolExecutionResult.ok_text("ok"),
    )
    ctx = _ctx()
    async for _ in run(
        "go",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=4,
    ):
        pass

    tool_resp_messages = [
        m
        for m in hist.rows
        if m.role == "user"
        and m.content
        and isinstance(m.content[0], ToolResponse)
    ]
    assert len(tool_resp_messages) == 1, (
        "serial tool responses must be consolidated into one user Message"
    )
    consolidated = tool_resp_messages[0].content
    by_id = {b.id: b.tool_name for b in consolidated}
    assert by_id == {"a": "read_file", "b": "run_command"}
    assert [b.id for b in consolidated] == sorted(by_id), (
        "tool result blocks must be in call_id order within the consolidated message"
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
async def test_run_vertex_google_search_true_reaches_gemini_provider_stream() -> None:
    prov = FakeProvider([[TextDelta(text="hi"), Done()]], name="gemini")
    async for _ in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        vertex_google_search=True,
    ):
        pass
    assert prov.vertex_google_search_calls
    assert all(prov.vertex_google_search_calls)


@pytest.mark.asyncio
async def test_run_vertex_google_search_defaults_false() -> None:
    prov = FakeProvider([[TextDelta(text="hi"), Done()]], name="gemini")
    async for _ in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass
    assert prov.vertex_google_search_calls
    assert not any(prov.vertex_google_search_calls)


@pytest.mark.asyncio
async def test_provider_grounding_event_translates_sources_and_queries_unchanged() -> None:
    prov = FakeProvider(
        [
            [
                ProviderGroundingEvent(
                    sources=[{"title": "Example", "url": "https://example.com"}],
                    search_queries=["weather today"],
                ),
                TextDelta(text="hi"),
                Done(),
            ]
        ],
        name="gemini",
    )
    events = []
    async for e in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        vertex_google_search=True,
    ):
        events.append(e)
    grounding_events = [e for e in events if isinstance(e, GroundingEvent)]
    assert len(grounding_events) == 1
    assert grounding_events[0].sources == [{"title": "Example", "url": "https://example.com"}]
    assert grounding_events[0].search_queries == ["weather today"]


@pytest.mark.asyncio
async def test_system_prompt_snapshot_includes_skills_memory_and_attachments() -> None:
    """Regression guard for PR #62: skills, memory, and attachments must survive into

    the actual system prompt sent through ``run()`` — not just be non-empty.
    """
    catalog = SessionAttachmentCatalog(session_id="t1")
    catalog.upsert(
        AttachmentRecord(
            attachment_id="att1",
            filename="report.pdf",
            mime_type="application/pdf",
            description="Q1 report",
            storage_path=".monkeybot/attachments/t1/att1",
        )
    )
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=["remembers the sky is blue"],
        skills=[SkillRef(name="pdf-skill", description="Handles PDFs")],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )
    prov = FakeProvider([[TextDelta(text="hi"), Done()]])
    events = []
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        attachment_catalog=catalog,
        max_turns=3,
    ):
        events.append(e)
    snaps = [e for e in events if isinstance(e, SystemPromptSnapshot)]
    assert snaps
    text = snaps[0].text
    assert "\n\n## Skills\n- pdf-skill" in text
    assert "## Memory index" in text
    assert "- remembers the sky is blue" in text
    assert "\n\n## Session attachments\n- att1 (report.pdf, application/pdf): Q1 report" in text


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
    err_blob = " ".join(
        (e.error or "")
        for e in events
        if isinstance(e, ToolCallResult) and isinstance(e.error, str)
    ).lower()
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


@pytest.mark.asyncio
async def test_tool_budget_recounts_after_assistant_append() -> None:
    """Budgeter seeds from a post-assistant provider count, not pre-stream preflight."""
    prov = CountingFakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
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
        tool_executor=RecordingExecutor(("tool-output", None)),
        max_turns=3,
    ):
        pass
    assert len(prov.count_snapshots) >= 2
    budget_messages = prov.count_snapshots[1]
    history_msgs = [m for m in budget_messages if m.role != "system"]
    assert any(
        m.role == "assistant"
        and any(isinstance(b, ToolRequest) for b in m.content)
        for m in history_msgs
    )


@pytest.mark.asyncio
async def test_compact_history_does_not_persist_synthetic_tool_repairs() -> None:
    """Compaction must reset unrepaired rows so in-memory repairs are not stored."""
    from monkeybot.core.messages.tool_integrity import repair_tool_turn_integrity

    preload: list[Message] = []
    for i in range(7):
        role = "user" if i % 2 == 0 else "assistant"
        preload.append(Message(role=role, content=[Text(text=f"turn-{i}")]))
    broken_tail = Message(
        role="assistant",
        content=[ToolRequest(id="c1", name="echo", args={})],
    )
    preload.append(broken_tail)
    assert len(preload) == 8

    repaired = repair_tool_turn_integrity(preload)
    assert len(repaired) == len(preload) + 1

    hist = FakeHistory(preload)
    prov = FakeProvider([[TextDelta(text=" compressed summary "), Done()]])
    turns = await _compact_history_if_needed(
        thread_id="t1",
        history=hist,
        provider=prov,
        model="gemini-2.5-flash",
    )
    assert turns == 1
    assert len(hist.reset_calls) == 1
    _, persisted = hist.reset_calls[0]
    synthetic_marker = "Tool call interrupted or result missing"
    for msg in persisted:
        for block in msg.content:
            if isinstance(block, ToolResponse):
                texts = [b.text for b in block.result if isinstance(b, Text)]
                assert synthetic_marker not in " ".join(texts)


@pytest.mark.asyncio
async def test_run_refreshes_tools_after_enable_mcp_same_turn() -> None:
    """After enable_mcp, the next provider_stream sees newly connected MCP tools."""

    class ToolRecordingProvider(FakeProvider):
        def __init__(self, scripted: list[list[object]]) -> None:
            super().__init__(scripted)
            self.tools_seen: list[list[str]] = []

        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
            vertex_google_search: bool = False,
        ) -> AsyncIterator[ProviderEvent]:
            self.tools_seen.append([t.name for t in tools])
            async for ev in super().stream(
                messages,
                tools,
                model=model,
                thinking_budget=thinking_budget,
                vertex_google_search=vertex_google_search,
            ):
                yield ev

    class CatalogMCP:
        def __init__(self) -> None:
            self._connected: dict[str, list[ToolDef]] = {}

        async def connect(self, *a: object, **k: object) -> list[ToolDef]:
            return []

        async def connect_streamable_http(self, *a: object, **k: object) -> list[ToolDef]:
            return []

        async def disconnect(self, name: str) -> None:
            self._connected.pop(name, None)

        async def call_tool(self, *a: object, **k: object) -> str:
            return ""

        def all_tools(self) -> list[ToolDef]:
            out: list[ToolDef] = []
            for tools in self._connected.values():
                out.extend(tools)
            return out

        def catalog_names(self) -> list[str]:
            return ["browser"]

        def known_server_names(self) -> list[str]:
            return ["browser"]

        def is_connected(self, name: str) -> bool:
            return name in self._connected

        def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
            return None

        async def connect_from_catalog(self, name: str) -> list[ToolDef]:
            defs = [ToolDef("browser__goto", "Navigate", {"type": "object"})]
            self._connected[name] = defs
            return defs

        async def load_from_config(self, *a: object, **k: object) -> None:
            return None

    class EnableThenAnswerExecutor(RecordingExecutor):
        def __init__(self, mcp: CatalogMCP) -> None:
            super().__init__(("ok", None))
            self._mcp = mcp

        @property
        def mcp(self) -> CatalogMCP:
            return self._mcp

        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            self.calls.append(call)
            if call.name == "enable_mcp":
                defs = await self._mcp.connect_from_catalog("browser")
                return ToolExecutionResult.ok_text(
                    json.dumps(
                        {
                            "ok": True,
                            "server": "browser",
                            "tools": [{"name": t.name, "description": t.description} for t in defs],
                        }
                    )
                )
            return self.result

    mcp = CatalogMCP()
    exe = EnableThenAnswerExecutor(mcp)
    prov = ToolRecordingProvider(
        [
            [
                ToolCall(name="enable_mcp", args={"name": "browser"}, call_id="c1"),
                Done(),
            ],
            [
                TextDelta(text="done"),
                UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0),
                Done(),
            ],
        ]
    )
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("enable_mcp", "Enable MCP", {}),
            ToolDef("run_command", "Run shell", {}),
        ],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        catalog_mcp_servers=("browser",),
    )
    events = []
    async for e in run(
        "open the site",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=5,
    ):
        events.append(e)

    assert isinstance(events[-1], TurnComplete)
    assert len(prov.tools_seen) >= 2
    assert "browser__goto" not in prov.tools_seen[0]
    assert "browser__goto" in prov.tools_seen[1]
    assert any(c.name == "enable_mcp" for c in exe.calls)
