"""Inner-loop count calls when the provider reports a prompt size."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall, UsageEvent
from monkeybot.core.runtime.events import TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.loop_usage import reported_prompt_tokens
from monkeybot.core.testing.mocks_provider import ScriptedProvider, fake_provider_prompt_tokens
from monkeybot.core.types.types_tools import ToolDef
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory


def _tool_turn(i: int, *, usage: int | None) -> list[object]:
    events: list[object] = [
        ToolCall(call_id=f"c{i}", name="run_command", args={"command": f"noop-{i}"}),
    ]
    if usage is not None:
        events.append(UsageEvent(input_tokens=usage, output_tokens=4))
    events.append(Done())
    return events


def _text_turn(*, usage: int | None) -> list[object]:
    events: list[object] = [TextDelta(text="done")]
    if usage is not None:
        events.append(UsageEvent(input_tokens=usage, output_tokens=4))
    events.append(Done())
    return events


class _ReportingProvider(ScriptedProvider):
    """Scripted turns whose usage report equals this provider's own count."""

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: object = None,
    ) -> AsyncIterator[object]:
        counted = fake_provider_prompt_tokens(messages, tools)
        async for event in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            hints=hints,  # type: ignore[arg-type]
        ):
            if isinstance(event, Done):
                yield UsageEvent(input_tokens=counted, output_tokens=4)
            yield event


class _FixedCountProvider(ScriptedProvider):
    def __init__(self, turns: list[list[object]], *, count: int) -> None:
        super().__init__(turns)  # type: ignore[arg-type]
        self._count = count

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: object = None,
    ) -> int:
        await super().count_input_tokens(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            hints=hints,  # type: ignore[arg-type]
        )
        return self._count


async def _drive(
    provider: ScriptedProvider,
    *,
    context_window_tokens: int = 200_000,
    tool_text: str = "ok",
) -> list[object]:
    events: list[object] = []
    async for event in run(
        "long task",
        _ctx(context_window_tokens=context_window_tokens),
        provider=provider,
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor((tool_text, None)),
        max_turns=8,
    ):
        events.append(event)
    assert isinstance(events[-1], TurnComplete)
    return events


@pytest.mark.asyncio
async def test_one_count_across_four_tool_turns_when_usage_matches() -> None:
    turns = [_tool_turn(i, usage=None) for i in range(4)]
    turns.append(_text_turn(usage=None))
    provider = _ReportingProvider(turns)  # type: ignore[arg-type]
    await _drive(provider)
    assert provider.stream_calls == 5
    assert len(provider.count_input_tokens_calls) == 1


@pytest.mark.asyncio
async def test_no_usage_keeps_counting_every_preflight_and_batch() -> None:
    turns = [_tool_turn(i, usage=None) for i in range(4)]
    turns.append(_text_turn(usage=None))
    provider = ScriptedProvider(turns)  # type: ignore[arg-type]
    await _drive(provider)
    assert provider.stream_calls == 5
    # Four tool batches each recount, plus one preflight per inner turn.
    assert len(provider.count_input_tokens_calls) == 9


@pytest.mark.asyncio
async def test_real_count_when_the_estimate_band_reaches_a_bar() -> None:
    provider = _FixedCountProvider(
        [
            _tool_turn(1, usage=68_000),
            _text_turn(usage=68_000),
        ],
        count=68_000,
    )
    await _drive(provider, context_window_tokens=100_000, tool_text="x" * 6_000)
    assert provider.stream_calls == 2
    assert len(provider.count_input_tokens_calls) == 2


def test_gemini_prompt_count_already_includes_cached_tokens() -> None:
    assert (
        reported_prompt_tokens(
            "gemini",
            input_tokens=1_200,
            cache_read_tokens=500,
            cache_creation_tokens=0,
        )
        == 1_200
    )
    assert (
        reported_prompt_tokens(
            "claude",
            input_tokens=100,
            cache_read_tokens=50,
            cache_creation_tokens=20,
        )
        == 170
    )
    assert (
        reported_prompt_tokens(
            "openai",
            input_tokens=100,
            cache_read_tokens=40,
            cache_creation_tokens=0,
        )
        == 140
    )
