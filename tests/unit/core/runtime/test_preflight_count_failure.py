"""A failed preflight count estimates locally and the turn still calls the model."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall, UsageEvent
from monkeybot.core.runtime.context_budget import (
    PROMPT_ESTIMATE_CHARS_PER_TOKEN,
    SUMMARY_TRIGGER_RATIO,
)
from monkeybot.core.runtime.events import ContextSummarizing, ContextUsage, Error, TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.prompt_ledger import request_chars
from monkeybot.core.testing.mocks_provider import ScriptedProvider, fake_provider_prompt_tokens
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory


def _local_estimate(messages: Sequence[Message], tools: Sequence[ToolDef]) -> int:
    chars = request_chars(messages, tools)
    if chars <= 0:
        return 0
    return math.ceil(chars / PROMPT_ESTIMATE_CHARS_PER_TOKEN)


def _tool_turn(i: int) -> list[object]:
    return [
        ToolCall(call_id=f"c{i}", name="run_command", args={"command": f"noop-{i}"}),
        Done(),
    ]


def _text_turn(text: str = "done") -> list[object]:
    return [TextDelta(text=text), Done()]


class _CountFailureProvider(ScriptedProvider):
    """Scripted turns whose token count fails on chosen 1-based call numbers.

    ``report`` inserts a usage event before each ``Done``. ``"estimate"`` is the
    local character estimate of the request being sent. ``"far"`` is three times
    that, outside the half-to-twice trust band.
    """

    def __init__(
        self,
        turns: list[list[object]],
        *,
        fail_on: set[int],
        count: int | None = None,
        report: str | None = None,
    ) -> None:
        super().__init__(turns)  # type: ignore[arg-type]
        self._fail_on = fail_on
        self._count = count
        self._report = report
        self._count_n = 0

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: object = None,
    ) -> int:
        self._count_n += 1
        call_n = self._count_n
        await super().count_input_tokens(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            hints=hints,  # type: ignore[arg-type]
        )
        if call_n in self._fail_on:
            raise RuntimeError("token count failed")
        if self._count is not None:
            return self._count
        return fake_provider_prompt_tokens(messages, tools)

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: object = None,
    ) -> AsyncIterator[object]:
        async for event in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            hints=hints,  # type: ignore[arg-type]
        ):
            if isinstance(event, Done) and self._report is not None:
                estimate = _local_estimate(messages, tools)
                reported = estimate * 3 if self._report == "far" else estimate
                yield UsageEvent(input_tokens=max(1, reported), output_tokens=4)
            yield event


def _compactable_history() -> list[Message]:
    return [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text="z" * 600)],
        )
        for i in range(8)
    ]


async def _drive(
    provider: ScriptedProvider,
    *,
    context_window_tokens: int = 200_000,
    history: FakeHistory | None = None,
    workspace_root: Path | None = None,
    max_turns: int = 8,
) -> list[object]:
    events: list[object] = []
    async for event in run(
        "long task",
        _ctx(context_window_tokens=context_window_tokens, workspace_root=workspace_root),
        provider=provider,
        history=history if history is not None else FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(("ok", None)),
        max_turns=max_turns,
    ):
        events.append(event)
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    return events


@pytest.mark.asyncio
async def test_failed_preflight_count_still_calls_the_model() -> None:
    provider = _CountFailureProvider([_text_turn()], fail_on={1})
    events = await _drive(provider)
    assert provider.stream_calls == 1
    assert len(provider.count_input_tokens_calls) == 1
    counted = provider.count_input_tokens_calls[0]
    usages = [event for event in events if isinstance(event, ContextUsage)]
    assert usages[0].estimated_tokens == _local_estimate(counted.messages, counted.tools)


@pytest.mark.asyncio
async def test_fallback_within_trust_skips_the_next_count() -> None:
    provider = _CountFailureProvider(
        [_tool_turn(1), _text_turn()],
        fail_on={1},
        report="estimate",
    )
    await _drive(provider)
    assert provider.stream_calls == 2
    assert len(provider.count_input_tokens_calls) == 1


@pytest.mark.asyncio
async def test_fallback_outside_trust_counts_again_on_the_next_call() -> None:
    provider = _CountFailureProvider(
        [_tool_turn(1), _text_turn()],
        fail_on={1, 2, 3},
        report="far",
    )
    await _drive(provider)
    assert provider.stream_calls == 2
    # First preflight, the post-tool recount, and the next call's preflight.
    assert len(provider.count_input_tokens_calls) == 3


@pytest.mark.asyncio
async def test_fallback_over_the_window_still_compacts(tmp_path: Path) -> None:
    provider = _CountFailureProvider(
        [_text_turn(" compressed summary "), _text_turn("final")],
        fail_on={1, 2, 3},
    )
    window = 1_600
    events = await _drive(
        provider,
        context_window_tokens=window,
        history=FakeHistory(_compactable_history()),
        workspace_root=tmp_path,
    )
    assert any(isinstance(event, ContextSummarizing) for event in events)
    usages = [event for event in events if isinstance(event, ContextUsage)]
    assert usages[0].estimated_tokens >= int(window * SUMMARY_TRIGGER_RATIO)
    assert provider.stream_calls == 2


@pytest.mark.asyncio
async def test_recount_after_compaction_can_fail_and_the_model_is_still_called(
    tmp_path: Path,
) -> None:
    provider = _CountFailureProvider(
        [_text_turn(" compressed summary "), _text_turn("final")],
        fail_on={2},
        count=2_000,
    )
    events = await _drive(
        provider,
        context_window_tokens=1_600,
        history=FakeHistory(_compactable_history()),
        workspace_root=tmp_path,
    )
    assert any(isinstance(event, ContextSummarizing) for event in events)
    assert provider.stream_calls == 2
    assert len(provider.count_input_tokens_calls) == 2
