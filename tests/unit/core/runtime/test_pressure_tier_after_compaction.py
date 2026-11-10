"""The shaping tier follows the latest prompt count, not the request peak.

Compaction rewrites history and recounts it. The peak on ``Usage`` stays at the
pre-compaction size for the rest of the request, and reading it kept older tool
results shaped after the prompt had already dropped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.runtime.background_summary import reset_pending_summaries
from monkeybot.core.runtime.context_budget import compute_context_pressure_tier
from monkeybot.core.runtime.events import (
    ContextSummarized,
    ContextSummarizing,
    ContextUsage,
    Error,
    TurnComplete,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import Text, ToolResponse
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory

WINDOW = 10_000
HIGH = 7_600
LOW = 2_500
OVER = 9_000
KEEP = "KEEP-LINE"
BULK = "MIDDLE-BULK"
FRESH = "FRESH-RESULT"
_SUMMARY_OPENING = "Output exactly the Markdown structure"
_TRUNCATED = "[... truncated for context budget"


@pytest.fixture(autouse=True)
async def _clear_pending_summaries() -> AsyncIterator[None]:
    await reset_pending_summaries()
    yield
    await reset_pending_summaries()


def _texts(messages: Sequence[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, Text):
                parts.append(block.text)
            elif isinstance(block, ToolResponse):
                parts.extend(inner.text for inner in block.result if isinstance(inner, Text))
    return "\n".join(parts)


def _shaped(text: str) -> bool:
    return "repeated" in text


def _assert_call_matches_last_usage(
    messages: Sequence[Message],
    events: Sequence[object],
    *,
    window: int,
) -> None:
    """Shaping on this call follows the last ContextUsage emitted before it."""
    text = _texts(messages)
    if KEEP not in text:
        return
    usages = [event.estimated_tokens for event in events if isinstance(event, ContextUsage)]
    assert usages, "provider call before any context usage"
    tier = compute_context_pressure_tier(usages[-1], window)
    if tier in ("moderate", "aggressive"):
        assert _shaped(text), f"tier {tier} at {usages[-1]} tokens left the old result verbatim"
        assert text.count(KEEP) < 40
    else:
        assert not _shaped(text), f"tier {tier} at {usages[-1]} tokens still shaped the old result"
        assert text.count(KEEP) == 40


class _Provider:
    """Main turns from a script. Summary calls answer themselves.

    ``count_input_tokens`` reports ``high`` while the bulk row is still in the
    prompt and ``low`` after compaction removes it. ``fail_low_count`` raises
    on that 1-based low count, which is how a recount failure is simulated.
    """

    def __init__(
        self,
        main: list[BaseException | str],
        events: list[object],
        *,
        high: int = HIGH,
        low: int = LOW,
        fail_low_count: int | None = None,
    ) -> None:
        self._main = list(main)
        self._events = events
        self._high = high
        self._low = low
        self._fail_low_count = fail_low_count
        self.low_counts = 0
        self.main_calls = 0
        self.summary_calls = 0
        self.name = "fake"

    def _is_summary(self, messages: Sequence[Message]) -> bool:
        return any(
            isinstance(block, Text) and block.text.startswith(_SUMMARY_OPENING)
            for message in messages
            for block in message.content
        )

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[TextDelta | ToolCall | Done]:
        del tools, model, thinking_budget, kwargs
        if self._is_summary(messages):
            self.summary_calls += 1
            yield TextDelta(text="summary")
            yield Done()
            return
        self.main_calls += 1
        _assert_call_matches_last_usage(messages, self._events, window=WINDOW)
        step = self._main.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step == "tool":
            yield ToolCall(
                call_id=f"c{self.main_calls}",
                name="run_command",
                args={"command": "noop"},
            )
            yield Done()
            return
        yield TextDelta(text=step)
        yield Done()

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        **kwargs: object,
    ) -> int:
        del tools, model, thinking_budget, kwargs
        if self._is_summary(messages) or BULK in _texts(messages):
            return self._high
        self.low_counts += 1
        if self._fail_low_count is not None and self.low_counts == self._fail_low_count:
            raise RuntimeError("recount failed")
        return self._low


def _old_result() -> str:
    return "\n".join([KEEP] * 40)


def _history(*, bulk: bool) -> list[Message]:
    """An old tool result, then a row compaction can remove, then a short tail.

    The old result is the head, so it survives compaction and sits outside the
    protected tail, which is the only place read-time shaping can touch it.
    """
    rows = [
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="old",
                    tool_name="run_command",
                    result=[Text(text=_old_result())],
                )
            ],
        ),
        Message(role="assistant", content=[Text(text="ack")]),
    ]
    if bulk:
        rows.append(Message(role="user", content=[Text(text=BULK + "m" * 20_000)]))
    rows.extend(
        [
            Message(role="assistant", content=[Text(text="tail-a")]),
            Message(role="user", content=[Text(text="tail-b")]),
        ]
    )
    return rows


async def _drive(
    tmp_path,
    provider: _Provider,
    events: list[object],
    rows: list[Message],
    *,
    tool_text: str = "ok",
    max_turns: int = 4,
) -> None:
    async for event in run(
        "continue",
        _ctx(workspace_root=tmp_path, context_window_tokens=WINDOW),
        provider=provider,  # type: ignore[arg-type]
        history=FakeHistory(rows),
        inspectors=[],
        tool_executor=RecordingExecutor((tool_text, None)),
        max_turns=max_turns,
    ):
        events.append(event)


async def test_tool_batch_compaction_stops_shaping_later_calls(tmp_path) -> None:
    events: list[object] = []
    provider = _Provider(["tool", "done"], events)
    await _drive(tmp_path, provider, events, _history(bulk=True))
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    assert provider.main_calls == 2
    assert provider.summary_calls >= 1
    assert any(
        isinstance(event, ContextSummarized) and event.turns_summarized > 0 for event in events
    )


async def test_preflight_compaction_sends_the_same_turn_unshaped(tmp_path) -> None:
    events: list[object] = []
    provider = _Provider(["done"], events, high=OVER)
    await _drive(tmp_path, provider, events, _history(bulk=True))
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    assert provider.main_calls == 1
    assert provider.summary_calls >= 1
    usages = [event.estimated_tokens for event in events if isinstance(event, ContextUsage)]
    assert usages[0] == OVER
    assert usages[-1] == LOW


async def test_pressure_without_compaction_still_shapes(tmp_path) -> None:
    events: list[object] = []
    provider = _Provider(["done"], events)
    await _drive(tmp_path, provider, events, _history(bulk=True))
    assert isinstance(events[-1], TurnComplete)
    assert provider.main_calls == 1
    assert not any(isinstance(event, ContextSummarized) for event in events)


async def test_compaction_that_summarizes_nothing_keeps_the_high_count(tmp_path) -> None:
    events: list[object] = []
    provider = _Provider(
        [RuntimeError("prompt is too long: 9000 tokens > 8000 maximum")],
        events,
        high=OVER,
        low=OVER,
    )
    # Three rows after the new user message: below the minimum that leaves a middle.
    short = [
        Message(role="user", content=[Text(text="one")]),
        Message(role="assistant", content=[Text(text="two")]),
    ]
    await _drive(tmp_path, provider, events, short)
    summarized = [event for event in events if isinstance(event, ContextSummarized)]
    assert [event.turns_summarized for event in summarized] == [0]
    usages = [event.estimated_tokens for event in events if isinstance(event, ContextUsage)]
    assert usages
    assert all(tokens == OVER for tokens in usages)
    assert provider.main_calls == 1
    assert any(isinstance(event, Error) for event in events)


async def test_failed_recount_after_compaction_does_not_use_the_peak(tmp_path) -> None:
    events: list[object] = []
    # Low counts: the post-compaction recount, the next preflight, then the
    # recount for the second tool batch. Only that third one fails.
    provider = _Provider(["tool", "tool", "done"], events, fail_low_count=3)
    fresh = FRESH + "x" * 12_000
    history = FakeHistory(_history(bulk=True))
    async for event in run(
        "continue",
        _ctx(workspace_root=tmp_path, context_window_tokens=WINDOW),
        provider=provider,  # type: ignore[arg-type]
        history=history,
        inspectors=[],
        tool_executor=RecordingExecutor((fresh, None)),
        max_turns=4,
    ):
        events.append(event)
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    assert provider.low_counts >= 3
    assert sum(isinstance(event, ContextSummarizing) for event in events) == 1
    fresh_results = [
        text
        for message in history.rows
        for block in message.content
        if isinstance(block, ToolResponse)
        and FRESH in (text := _texts([Message(role="user", content=[block])]))
    ]
    # The first batch ran at the real 76% count and was trimmed. The batch whose
    # recount failed must keep the full result: the fallback is the 25% recount,
    # and the 76% peak would have trimmed it again.
    assert len(fresh_results) == 2
    assert _TRUNCATED in fresh_results[0]
    assert fresh in fresh_results[1]
    assert _TRUNCATED not in fresh_results[1]
