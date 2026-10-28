"""Background history summaries: start in the 70% band, apply at the bar."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.runtime import background_summary as bg
from monkeybot.core.runtime.background_summary import (
    maybe_start,
    reset_pending_summaries,
    wait_idle,
)
from monkeybot.core.runtime.context_budget import PRESSURE_MODERATE_RATIO, SUMMARY_TRIGGER_RATIO
from monkeybot.core.runtime.history_compaction import (
    _WORKSPACE_STATE_HEADING,
    _compact_history_if_needed,
    _finalize_summary_row,
    _summarization_model_id,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import SystemNotification, Text
from monkeybot.providers._stream_marks import (
    StreamMarks,
    bind_stream_marks,
    mark_content,
    reset_stream_marks,
)
from testkit.context import _ctx as _loop_ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.provider import FakeProvider

WINDOW = 10_000
_FLOOR = max(1, int(WINDOW * PRESSURE_MODERATE_RATIO))
_CAP = max(1, int(WINDOW * SUMMARY_TRIGGER_RATIO))
_IN_BAND = _FLOOR


@pytest.fixture(autouse=True)
async def _clear_pending_summaries() -> AsyncIterator[None]:
    await reset_pending_summaries()
    yield
    await reset_pending_summaries()


def _ctx(thread_id: str = "t1", window: int = WINDOW) -> TurnContext:
    return TurnContext(
        thread_id=thread_id,
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


def _rows(count: int = 8) -> list[Message]:
    rows = [
        Message(
            role="user" if i % 2 == 0 else "assistant", content=[Text(text=f"fact {i} " + "x" * 80)]
        )
        for i in range(count)
    ]
    rows.insert(
        1,
        Message(
            role="user",
            content=[SystemNotification(notification_type="verifierVerdict", msg="verdict-ok")],
        ),
    )
    return rows


class MemoryHistory:
    def __init__(self, rows: list[Message] | None = None) -> None:
        self.rows = list(rows) if rows is not None else _rows()

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        del thread_id
        if limit is None:
            return list(self.rows)
        return list(self.rows[-limit:]) if limit > 0 else []

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del thread_id, turn_id, message_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.rows = list(messages)


class Scripted:
    """Yields one text per call. A ``BaseException`` entry is raised instead."""

    def __init__(self, script: Sequence[str | BaseException]) -> None:
        self._script = list(script)
        self.calls = 0

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[TextDelta | Done]:
        del messages, tools, model, thinking_budget
        item: str | BaseException = (
            self._script[self.calls] if self.calls < len(self._script) else "extra"
        )
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        yield TextDelta(text=item)
        yield Done()


def _summary_text(rows: Sequence[Message]) -> str:
    parts: list[str] = []
    for message in rows:
        for block in message.content:
            if isinstance(block, Text) and block.text.startswith("[Context Summary]:"):
                parts.append(block.text)
    return "\n".join(parts)


def _has_verdict(rows: Sequence[Message]) -> bool:
    return any(
        isinstance(block, SystemNotification)
        and block.notification_type == "verifierVerdict"
        and block.msg == "verdict-ok"
        for message in rows
        for block in message.content
    )


async def _compact(
    history: MemoryHistory, provider: Scripted, ctx: TurnContext, **kwargs: object
) -> int:
    trailer = kwargs.get("workspace_trailer")
    return await _compact_history_if_needed(
        thread_id=ctx.thread_id,
        history=history,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model=_summarization_model_id(ctx),
        window_tokens=ctx.context_window_tokens,
        workspace_trailer=trailer if isinstance(trailer, str) else None,
    )


async def test_maybe_start_runs_once_inside_the_band() -> None:
    assert _FLOOR < _CAP
    history = MemoryHistory()
    provider = Scripted(["bg-summary"])
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    assert provider.calls == 1


async def test_maybe_start_ignores_preflight_outside_the_band() -> None:
    class Boom(MemoryHistory):
        async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
            del thread_id, limit
            raise AssertionError("history.load must not run outside the band")

    provider = Scripted(["bg-summary"])
    ctx = _ctx()
    await maybe_start(ctx, Boom(), provider, _FLOOR - 1)  # type: ignore[arg-type]
    await maybe_start(ctx, Boom(), provider, _CAP)  # type: ignore[arg-type]
    assert provider.calls == 0
    assert ctx.thread_id not in bg._pending


async def test_ready_summary_is_applied_without_another_provider_call() -> None:
    history = MemoryHistory()
    provider = Scripted(["bg-summary", "inline-summary"])
    ctx = _ctx()
    trailer = f"{_WORKSPACE_STATE_HEADING}\n- Files written this session: a.py"
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    assert provider.calls == 1
    await history.append(ctx.thread_id, Message(role="user", content=[Text(text="kept-after")]))
    turns = await _compact(history, provider, ctx, workspace_trailer=trailer)
    assert provider.calls == 1
    assert turns > 0
    assert history.rows[-1].content == [Text(text="kept-after")]
    summary = _summary_text(history.rows)
    assert "bg-summary" in summary
    assert "inline-summary" not in summary
    assert "a.py" in summary
    assert _has_verdict(history.rows)


async def test_changed_prefix_falls_back_to_inline_summary() -> None:
    history = MemoryHistory()
    provider = Scripted(["bg-summary", "inline-summary"])
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    history.rows[0] = Message(role="user", content=[Text(text="edited head")])
    turns = await _compact(history, provider, ctx)
    assert turns > 0
    assert provider.calls == 2
    summary = _summary_text(history.rows)
    assert "inline-summary" in summary
    assert "bg-summary" not in summary


async def test_failed_background_job_falls_back_to_inline_summary() -> None:
    history = MemoryHistory()
    provider = Scripted([RuntimeError("boom"), "inline-summary"])
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    turns = await _compact(history, provider, ctx)
    assert turns > 0
    assert provider.calls == 2
    assert "inline-summary" in _summary_text(history.rows)


class _BlockingFirst:
    """First stream waits. Later streams yield ``later`` immediately."""

    def __init__(self, later: str = "inline-summary") -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._later = later

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[object],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[TextDelta | Done]:
        del messages, tools, model, thinking_budget
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
            yield TextDelta(text="bg-summary")
        else:
            yield TextDelta(text=self._later)
        yield Done()


async def _run_until_blocked(task: asyncio.Task[object]) -> None:
    """Let ``task`` reach its first real wait. Fails if it finishes instead."""
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()


async def test_overgrown_tail_falls_back_without_waiting() -> None:
    """A large append makes the prepared cut stale. Compact must inline immediately.

    Waiting for that summary would hold the pressured turn, then write a summary
    that leaves the new rows verbatim. The gate is never released: if compact
    awaits the stale job, this test times out.
    """
    history = MemoryHistory()
    provider = _BlockingFirst()
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await provider.entered.wait()
    await history.append(ctx.thread_id, Message(role="user", content=[Text(text="y" * 12_000)]))
    turns = await asyncio.wait_for(_compact(history, provider, ctx), timeout=2)
    assert turns > 0
    assert provider.calls == 2
    summary = _summary_text(history.rows)
    assert "inline-summary" in summary
    assert "bg-summary" not in summary
    assert any(
        isinstance(block, Text) and block.text.startswith("y" * 100)
        for row in history.rows
        for block in row.content
    )


async def test_row_appended_while_streaming_is_kept_when_the_tail_still_fits() -> None:
    history = MemoryHistory()
    provider = _BlockingFirst()
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await provider.entered.wait()

    async def _apply() -> int:
        return await _compact(history, provider, ctx)

    applying = asyncio.create_task(_apply())
    await _run_until_blocked(applying)
    await history.append(ctx.thread_id, Message(role="user", content=[Text(text="kept-during")]))
    provider.release.set()
    turns = await asyncio.wait_for(applying, timeout=2)
    assert turns > 0
    assert provider.calls == 1
    assert history.rows[-1].content == [Text(text="kept-during")]
    assert "bg-summary" in _summary_text(history.rows)


async def test_minimum_tail_over_budget_is_not_restarted() -> None:
    """The split's minimum tail can already exceed the budget. That job is the
    summary for those rows; the next in-band turn must not cancel it and start
    the same one again.
    """
    history = MemoryHistory()
    ctx = _ctx()
    huge = "h" * 20_000
    await history.append(ctx.thread_id, Message(role="user", content=[Text(text=huge)]))
    await history.append(ctx.thread_id, Message(role="assistant", content=[Text(text=huge)]))
    provider = _BlockingFirst()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await provider.entered.wait()
    first_pending = bg._pending[ctx.thread_id]
    assert first_pending.suffix_tokens > bg._tail_budget(ctx.context_window_tokens)
    first = first_pending.task
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert provider.calls == 1
    assert bg._pending[ctx.thread_id].task is first
    assert not first.cancelled()
    provider.release.set()
    await wait_idle(ctx.thread_id)
    turns = await _compact(history, provider, ctx)
    assert turns > 0
    assert provider.calls == 1
    assert "bg-summary" in _summary_text(history.rows)


async def test_deleted_tail_falls_back_to_inline_summary() -> None:
    """Rewind drops the newest rows. The prepared cut still matches the prefix,
    but those rows are now the tail an inline split would keep.
    """
    history = MemoryHistory()
    provider = Scripted(["bg-summary", "inline-summary"])
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    history.rows = history.rows[:-3]
    turns = await _compact(history, provider, ctx)
    assert turns > 0
    assert provider.calls == 2
    summary = _summary_text(history.rows)
    assert "inline-summary" in summary
    assert "bg-summary" not in summary


async def test_cancelled_background_job_falls_back_to_inline() -> None:
    history = MemoryHistory()
    provider = _BlockingFirst()
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await provider.entered.wait()
    bg._pending[ctx.thread_id].task.cancel()
    turns = await asyncio.wait_for(_compact(history, provider, ctx), timeout=2)
    assert turns > 0
    assert "inline-summary" in _summary_text(history.rows)
    assert "bg-summary" not in _summary_text(history.rows)


async def test_wait_idle_propagates_waiter_cancellation() -> None:
    history = MemoryHistory()
    provider = _BlockingFirst()
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await provider.entered.wait()
    waiter = asyncio.create_task(wait_idle(ctx.thread_id))
    await _run_until_blocked(waiter)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    pending = bg._pending[ctx.thread_id]
    assert not pending.task.cancelled()
    provider.release.set()
    await wait_idle(ctx.thread_id)
    assert pending.task.done()
    assert not pending.task.cancelled()


async def test_background_job_does_not_write_the_user_stream_marks() -> None:
    class Marking(Scripted):
        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[object],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> AsyncIterator[TextDelta | Done]:
            mark_content("text")
            mark_content("thinking")
            async for event in super().stream(
                messages,
                tools,
                model=model,
                thinking_budget=thinking_budget,
            ):
                yield event

    history = MemoryHistory()
    provider = Marking(["bg-summary"])
    ctx = _ctx()
    marks = StreamMarks(started=time.perf_counter())
    token = bind_stream_marks(marks)
    try:
        await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
        await wait_idle(ctx.thread_id)
    finally:
        reset_stream_marks(token)
    assert provider.calls == 1
    assert marks.first_at is None
    assert marks.first_kind is None


async def test_stale_job_is_replaced_when_the_tail_exceeds_its_budget() -> None:
    history = MemoryHistory()
    provider = Scripted(["summary-one", "summary-two"])
    ctx = _ctx()
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    await history.append(
        ctx.thread_id,
        Message(role="user", content=[Text(text="y" * 12_000)]),
    )
    await maybe_start(ctx, history, provider, _IN_BAND)  # type: ignore[arg-type]
    await wait_idle(ctx.thread_id)
    turns = await _compact(history, provider, ctx)
    assert provider.calls == 2
    assert turns > 0
    summary = _summary_text(history.rows)
    assert "summary-two" in summary
    assert "summary-one" not in summary
    assert any(
        isinstance(block, Text) and block.text.startswith("y" * 100)
        for row in history.rows
        for block in row.content
    )


def test_finalize_uses_the_trailer_from_apply_time() -> None:
    row = _finalize_summary_row(
        "hello\n\n## Workspace state (harness-generated)\n- old",
        "## Workspace state (harness-generated)\n- new",
    )
    text = row.content[0].text
    assert isinstance(text, str)
    assert "- new" in text
    assert "- old" not in text
    assert "Standing instructions" in text


async def test_full_registry_drops_the_oldest_job() -> None:
    gate = asyncio.Event()

    class Blocking(Scripted):
        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[object],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> AsyncIterator[TextDelta | Done]:
            del messages, tools, model, thinking_budget
            self.calls += 1
            await gate.wait()
            yield TextDelta(text="x")
            yield Done()

    history = MemoryHistory()
    provider = Blocking([])
    extra = bg._MAX_PENDING + 1
    for index in range(extra):
        await maybe_start(
            _ctx(f"t{index}"),
            history,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            _IN_BAND,
        )
    assert "t0" not in bg._pending
    assert f"t{bg._MAX_PENDING}" in bg._pending
    assert len(bg._pending) == bg._MAX_PENDING


async def test_turn_schedules_background_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    async def spy(
        ctx: TurnContext,
        history: object,
        provider: object,
        preflight: int,
    ) -> None:
        del ctx, history, provider
        seen.append(preflight)

    monkeypatch.setattr("monkeybot.core.runtime.turn_loop.maybe_start", spy)
    events = [
        event
        async for event in run(
            "hello",
            _loop_ctx(),
            provider=FakeProvider([[TextDelta(text="pong"), Done()]]),
            history=FakeHistory(),
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=2,
        )
    ]
    assert seen and seen[0] > 0
    assert any(type(event).__name__ == "TurnComplete" for event in events)
