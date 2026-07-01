"""Tests for optional TranscriptWriter capture in monkeybot.core.runtime.loop.run."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.types_tools import ToolDef


def _ctx(*, workspace_root: Path | None = None) -> TurnContext:
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
    )


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
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, thinking_budget, model
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
        del messages, tools, model, thinking_budget
        return 10


class RecordingExecutor:
    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        del ctx, call
        return ToolExecutionResult.ok_text("ok")


class AllowInspector:
    async def check(self, call: Any, ctx: Any) -> Any:
        del call, ctx
        from monkeybot.core.tools.inspector import Decision

        return Decision(kind="allow")


def _read_lines(path: Path) -> list[dict[str, object]]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_run_without_transcript_writer_writes_nothing(tmp_path: Path) -> None:
    prov = FakeProvider([[TextDelta(text="hi"), UsageEvent(input_tokens=1, output_tokens=1), Done()]])
    async for _ in run(
        "hello",
        _ctx(workspace_root=tmp_path),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass
    assert not (tmp_path / ".monkeybot" / "transcripts").exists()


@pytest.mark.asyncio
async def test_run_with_transcript_writer_captures_provider_request_and_response(
    tmp_path: Path,
) -> None:
    prov = FakeProvider(
        [[TextDelta(text="hi there"), UsageEvent(input_tokens=5, output_tokens=2), Done()]]
    )
    writer = TranscriptWriter("sess-t", workspace_root=tmp_path)

    async for _ in run(
        "hello",
        _ctx(workspace_root=tmp_path),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        transcript_writer=writer,
    ):
        pass

    lines = _read_lines(writer.path)
    types = [line["type"] for line in lines]
    assert "ProviderRequest" in types
    assert "ProviderResponse" in types

    req = next(line for line in lines if line["type"] == "ProviderRequest")
    assert req["model"] == "gemini-2.5-flash"
    assert isinstance(req["messages"], list) and len(req["messages"]) >= 1

    resp = next(line for line in lines if line["type"] == "ProviderResponse")
    assert resp["text"] == "hi there"
    assert resp["usage"]["input_tokens"] == 5
    assert resp["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_run_with_transcript_writer_captures_agent_events(tmp_path: Path) -> None:
    prov = FakeProvider([[TextDelta(text="hi"), UsageEvent(input_tokens=1, output_tokens=1), Done()]])
    writer = TranscriptWriter("sess-t2", workspace_root=tmp_path)

    events = []
    async for evt in run(
        "hello",
        _ctx(workspace_root=tmp_path),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        transcript_writer=writer,
    ):
        events.append(evt)

    lines = _read_lines(writer.path)
    types = [line["type"] for line in lines]
    # Loop only writes provider request/response directly; AgentEvent teeing to the
    # transcript happens at the gateway layer (GatewayLoopPort.start_turn), not here.
    assert "ProviderRequest" in types
    assert "ProviderResponse" in types
    assert len(events) > 0


@pytest.mark.asyncio
async def test_run_with_transcript_writer_writes_message_deltas_on_tool_loop(
    tmp_path: Path,
) -> None:
    """Each inner turn should append only new provider messages, not the full history."""
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"cmd": "echo hi"}),
                UsageEvent(input_tokens=5, output_tokens=2),
                Done(),
            ],
            [TextDelta(text="done"), UsageEvent(input_tokens=3, output_tokens=1), Done()],
        ]
    )
    writer = TranscriptWriter("sess-delta", workspace_root=tmp_path)

    async for _ in run(
        "hello",
        _ctx(workspace_root=tmp_path),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=5,
        transcript_writer=writer,
    ):
        pass

    lines = _read_lines(writer.path)
    requests = [line for line in lines if line["type"] == "ProviderRequest"]
    assert len(requests) == 2

    first, second = requests
    assert first["inner_turn"] == 1
    assert first["message_offset"] == 0
    assert "tools" in first
    assert len(first["messages"]) >= 1

    assert second["inner_turn"] == 2
    assert second["message_offset"] == len(first["messages"])
    assert "tools" not in second
    assert len(second["messages"]) >= 1
    assert len(second["messages"]) < len(first["messages"]) + len(second["messages"])
