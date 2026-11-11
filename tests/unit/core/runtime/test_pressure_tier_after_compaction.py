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
    # Moderate starts a background summary and leaves sent rows verbatim.
    if tier == "aggressive":
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


async def test_moderate_band_does_not_shape(tmp_path) -> None:
    """76% is under the compaction bar, so the old result stays verbatim."""
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


_WIDE = 100_000
_BAND = 76_000
_BAR = 85_000
_AFTER = 25_000


def _row_texts(messages: Sequence[Message]) -> list[str]:
    return [_texts([message]) for message in messages if message.role != "system"]


class _WideProvider:
    """Scripted main turns. Counts stay at 76% through two tool turns, then hit 85%.

    ``count`` fixes every count, for a history that cannot be summarized.
    The two tool turns each take a preflight and a post-tool recount, all at
    76%. The next count is that recount's successor, the preflight that crosses
    the bar. Later counts are the size after compaction.
    """

    def __init__(
        self,
        main: list[str],
        events: list[object],
        *,
        count: int | None = None,
    ) -> None:
        self._main = list(main)
        self._events = events
        self._fixed = count
        self._after_two = 0
        self.main_calls = 0
        self.counts: list[int] = []
        self.rows: list[list[str]] = []
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
            yield TextDelta(text="summary")
            yield Done()
            return
        self.main_calls += 1
        self.rows.append(_row_texts(messages))
        step = self._main.pop(0)
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
        if self._fixed is not None:
            value = self._fixed
        elif self._is_summary(messages) or self.main_calls < 2:
            value = _BAND
        elif self._after_two == 0:
            self._after_two += 1
            value = _BAND
        elif self._after_two == 1:
            self._after_two += 1
            value = _BAR
        else:
            value = _AFTER
        self.counts.append(value)
        return value


async def _drive_wide(
    tmp_path,
    provider: _WideProvider,
    events: list[object],
    rows: list[Message],
    *,
    max_turns: int = 4,
) -> None:
    async for event in run(
        "continue",
        _ctx(workspace_root=tmp_path, context_window_tokens=_WIDE),
        provider=provider,  # type: ignore[arg-type]
        history=FakeHistory(rows),
        inspectors=[],
        tool_executor=RecordingExecutor(("ok", None)),
        max_turns=max_turns,
    ):
        events.append(event)


async def test_tool_batch_in_the_band_compacts_at_85(tmp_path) -> None:
    """A 76% tool batch does not compact, and the next call at 85% compacts once."""
    events: list[object] = []
    provider = _WideProvider(["tool", "tool", "done"], events)
    await _drive_wide(tmp_path, provider, events, _history(bulk=True))
    assert isinstance(events[-1], TurnComplete), provider.counts
    assert not any(isinstance(event, Error) for event in events), provider.counts
    assert provider.main_calls == 3, provider.counts
    assert provider.rows[1][: len(provider.rows[0])] == provider.rows[0]
    kept = [row for row in provider.rows[0] if KEEP in row]
    assert kept and kept[0].count(KEEP) == 40
    assert kept[0] in provider.rows[1]
    summarizing = [event for event in events if isinstance(event, ContextSummarizing)]
    assert len(summarizing) == 1, provider.counts
    prior = [
        event.estimated_tokens
        for event in events[: events.index(summarizing[0])]
        if isinstance(event, ContextUsage)
    ]
    assert prior[-1] == _BAR, prior


async def test_unsplittable_history_at_the_bar_is_shaped(tmp_path) -> None:
    """85% with nothing to summarize is the aggressive tier, so the old result is shaped."""
    events: list[object] = []
    provider = _WideProvider(["done"], events, count=_BAR)
    short = [
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
    await _drive_wide(tmp_path, provider, events, short, max_turns=2)
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    assert provider.main_calls == 1
    assert not any(isinstance(event, ContextSummarizing) for event in events)
    kept = [row for row in provider.rows[0] if KEEP in row]
    assert len(kept) == 1
    assert _shaped(kept[0])
    assert kept[0].count(KEEP) < 40
