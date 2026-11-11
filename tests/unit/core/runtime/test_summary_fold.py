"""The summarizer prompt stays inside the summary budget."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.runtime.events import AssistantDelta, ContextSummarized, Error
from monkeybot.core.runtime.background_summary import (
    maybe_start,
    reset_pending_summaries,
    wait_idle,
)
from monkeybot.core.runtime.context_budget import (
    PRESSURE_MODERATE_RATIO,
    estimate_tokens,
    summary_input_budget,
)
from monkeybot.core.runtime.history_compaction import (
    _COMPACTION_SUMMARY_SYSTEM,
    _compact_history_if_needed,
    _generate_summary_text,
    _middle_summary_lines,
    _summarize_history,
    _summary_request_tokens,
    _summary_user_text,
    split_messages_for_compaction,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import Text
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory

WINDOW = 8_000


@pytest.fixture(autouse=True)
async def _clear_pending_summaries() -> AsyncIterator[None]:
    await reset_pending_summaries()
    yield
    await reset_pending_summaries()


class RecordingProvider:
    """Records each summary request. ``fail_at`` raises on that 1-based call."""

    def __init__(self, *, fail_at: int | None = None, reply: str = "folded") -> None:
        self.fail_at = fail_at
        self.reply = reply
        self.calls = 0
        self.messages: list[list[Message]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[TextDelta | Done]:
        del tools, model, thinking_budget, kwargs
        self.messages.append(list(messages))
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("summary failed")
        yield TextDelta(text=self.reply)
        yield Done()


def _request_tokens(messages: Sequence[Message]) -> int:
    return sum(
        estimate_tokens(block.text)
        for message in messages
        for block in message.content
        if isinstance(block, Text)
    )


def _rows(count: int, chars: int) -> list[Message]:
    return [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=f"{i}-" + "w" * chars)],
        )
        for i in range(count)
    ]


def test_summary_budget_is_the_trigger_ratio() -> None:
    assert summary_input_budget(200_000) == 170_000
    assert summary_input_budget(1) == 1


async def test_one_chunk_matches_the_single_prompt() -> None:
    middle = _rows(4, 40)
    facts = "Intent: keep the answer short."
    provider = RecordingProvider(reply="one")
    text = await _generate_summary_text(
        provider, "m", middle, window_tokens=200_000, intent_facts=facts
    )
    assert text == "one"
    assert provider.calls == 1
    system, user = provider.messages[0]
    assert system.content[0].text == _COMPACTION_SUMMARY_SYSTEM  # type: ignore[union-attr]
    lines = _middle_summary_lines(middle, window_tokens=200_000)
    assert user.content[0].text == _summary_user_text(lines, facts)  # type: ignore[union-attr]


async def test_over_budget_middle_stays_under_the_budget_and_resets_once() -> None:
    rows = _rows(8, 16_000)
    history = FakeHistory(rows)
    provider = RecordingProvider()
    turns = await _summarize_history(
        "t1",
        history,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        "m",
        window_tokens=WINDOW,
    )
    budget = summary_input_budget(WINDOW)
    assert turns > 0
    assert provider.calls > 1
    assert len(history.reset_calls) == 1
    assert all(_request_tokens(messages) <= budget for messages in provider.messages)
    _, middle, _ = split_messages_for_compaction(rows, window_tokens=WINDOW)
    lines = _middle_summary_lines(middle, window_tokens=WINDOW)
    assert _summary_request_tokens(lines, None) > budget


async def test_one_oversized_row_is_trimmed_and_the_fold_finishes() -> None:
    middle = [Message(role="user", content=[Text(text="Q" * 200_000)])]
    provider = RecordingProvider(reply="trimmed")
    text = await _generate_summary_text(provider, "m", middle, window_tokens=WINDOW)
    budget = summary_input_budget(WINDOW)
    assert text == "trimmed"
    assert provider.calls >= 1
    assert all(_request_tokens(messages) <= budget for messages in provider.messages)
    sent = provider.messages[0][1].content[0].text  # type: ignore[union-attr]
    assert "Q" * 200_000 not in sent
    assert "truncated for context budget" in sent


async def test_a_failed_later_call_leaves_history_untouched() -> None:
    rows = _rows(8, 16_000)
    history = FakeHistory(rows)
    before = list(history.rows)
    provider = RecordingProvider(fail_at=2)
    with pytest.raises(RuntimeError, match="summary failed"):
        await _summarize_history(
            "t1",
            history,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            "m",
            window_tokens=WINDOW,
        )
    assert provider.calls == 2
    assert history.reset_calls == []
    assert history.rows == before


async def test_background_fold_is_applied_without_another_call() -> None:
    window = 10_000
    history = FakeHistory(_rows(8, 16_000))
    provider = RecordingProvider(reply="from-background")
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        context_window_tokens=window,
    )
    floor = max(1, int(window * PRESSURE_MODERATE_RATIO))
    await maybe_start(ctx, history, provider, floor)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    calls = provider.calls
    assert calls > 1
    turns = await _compact_history_if_needed(
        thread_id=ctx.thread_id,
        history=history,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model=ctx.model,
        window_tokens=window,
    )
    assert turns > 0
    assert provider.calls == calls
    assert any(
        isinstance(block, Text) and "from-background" in block.text
        for message in history.rows
        for block in message.content
    )


class _FailingSummarizer:
    """Raises on the summary call and answers the main call. Counts stay high."""

    name = "fake"

    def __init__(self) -> None:
        self.summary_sizes: list[int] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[TextDelta | Done]:
        del tools, model, thinking_budget, kwargs
        system = ""
        if messages and isinstance(messages[0].content[0], Text):
            system = messages[0].content[0].text
        if "## Objective" in system:
            self.summary_sizes.append(_request_tokens(messages))
            raise RuntimeError("summary failed")
        yield TextDelta(text="ok")
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
        del messages, tools, model, thinking_budget, kwargs
        return 100_000


async def test_a_failing_summarizer_never_grows_the_request(tmp_path) -> None:
    window = 4_000
    budget = summary_input_budget(window)
    history = FakeHistory(_rows(8, 20_000))
    provider = _FailingSummarizer()
    ctx = _ctx(workspace_root=tmp_path, context_window_tokens=window)
    for _turn in range(3):
        async for _event in run(
            "hello",
            ctx,
            provider=provider,  # type: ignore[arg-type]
            history=history,
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=2,
        ):
            pass
    assert len(provider.summary_sizes) >= 3
    assert max(provider.summary_sizes) <= budget
    _, middle, _ = split_messages_for_compaction(history.rows, window_tokens=window)
    lines = _middle_summary_lines(middle, window_tokens=window)
    assert _summary_request_tokens(lines, None) > budget


async def test_a_failing_inline_summary_still_answers_and_keeps_history(tmp_path) -> None:
    window = 4_000
    rows = _rows(8, 20_000)
    history = FakeHistory(rows)
    before = list(history.rows)
    provider = _FailingSummarizer()
    events = [
        event
        async for event in run(
            "hello",
            _ctx(workspace_root=tmp_path, context_window_tokens=window),
            provider=provider,  # type: ignore[arg-type]
            history=history,
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=2,
        )
    ]
    text = "".join(event.delta for event in events if isinstance(event, AssistantDelta))
    summarized = [event for event in events if isinstance(event, ContextSummarized)]
    assert text == "ok"
    assert not any(isinstance(event, Error) for event in events)
    assert summarized
    assert all(event.turns_summarized == 0 for event in summarized)
    assert history.reset_calls == []
    assert history.rows[: len(before)] == before
    assert len(history.rows) == len(before) + 2


class _TooLongError(Exception):
    def __init__(self) -> None:
        super().__init__("prompt is too long")


class _RejectOnce(RecordingProvider):
    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[TextDelta | Done]:
        del tools, model, thinking_budget, kwargs
        self.messages.append(list(messages))
        self.calls += 1
        if self.calls == 1:
            raise _TooLongError()
        yield TextDelta(text="shrunk")
        yield Done()


async def test_a_size_rejection_halves_the_chunk_and_retries() -> None:
    middle = [Message(role="user", content=[Text(text="Q" * 200_000)])]
    provider = _RejectOnce()
    text = await _generate_summary_text(provider, "m", middle, window_tokens=WINDOW)
    assert text == "shrunk"
    assert provider.calls == 2
    first = provider.messages[0][1].content[0].text  # type: ignore[union-attr]
    second = provider.messages[1][1].content[0].text  # type: ignore[union-attr]
    assert len(second) < len(first)
    budget = summary_input_budget(WINDOW)
    assert _request_tokens(provider.messages[1]) <= budget // 2


class _AlwaysTooLong:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[TextDelta | Done]:
        del messages, tools, model, thinking_budget, kwargs
        self.calls += 1
        raise _TooLongError()
        yield TextDelta(text="")  # pragma: no cover - keeps this an async generator


async def test_three_halvings_then_the_size_rejection_propagates() -> None:
    history = FakeHistory(_rows(8, 16_000))
    before = list(history.rows)
    provider = _AlwaysTooLong()
    with pytest.raises(_TooLongError):
        await _summarize_history(
            "t1",
            history,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            "m",
            window_tokens=WINDOW,
        )
    assert provider.calls == 4
    assert history.reset_calls == []
    assert history.rows == before
