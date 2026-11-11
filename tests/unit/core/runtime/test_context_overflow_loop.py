"""A provider size rejection compacts once and sends again."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.runtime.events import AssistantDelta, Error, HarnessIntervention
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.turn_loop import CONTEXT_OVERFLOW_ERROR
from monkeybot.core.types.content_blocks import Text
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory

_SUMMARY_OPENING = "Output exactly the Markdown structure"


class _TooLongError(Exception):
    def __init__(self) -> None:
        super().__init__("prompt is too long: 9000 tokens > 8000 maximum")


class _Provider:
    def __init__(
        self,
        main: list[BaseException | str],
        *,
        counted: int = 1_000,
        summary_error: str | None = None,
    ) -> None:
        self._main = list(main)
        self._counted = counted
        # ``runtime`` fails the summary once. ``too_long`` is a size rejection
        # on every summary call, so the fold halves and then gives up.
        self._summary_error = summary_error
        self.summary_calls = 0
        self.main_calls = 0
        self.name = "fake"

    def _summary_failure(self) -> BaseException | None:
        if self._summary_error == "runtime":
            return RuntimeError("summary failed")
        if self._summary_error == "too_long":
            return _TooLongError()
        return None

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
        opening = ""
        if messages and isinstance(messages[0].content[0], Text):
            opening = messages[0].content[0].text
        if opening.startswith(_SUMMARY_OPENING):
            self.summary_calls += 1
            failure = self._summary_failure()
            if failure is not None:
                raise failure
            yield TextDelta(text="summary")
            yield Done()
            return
        self.main_calls += 1
        step = self._main.pop(0)
        if isinstance(step, BaseException):
            raise step
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
        del messages, tools, model, thinking_budget, kwargs
        return self._counted


def _rows(count: int, chars: int = 40) -> list[Message]:
    return [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=f"{i}" + "z" * chars)],
        )
        for i in range(count)
    ]


def _text(events: Sequence[object]) -> str:
    return "".join(event.delta for event in events if isinstance(event, AssistantDelta))


async def _drive(
    tmp_path, provider: _Provider, rows: list[Message], *, window: int
) -> tuple[list[object], FakeHistory]:
    history = FakeHistory(rows)
    events = [
        event
        async for event in run(
            "hello",
            _ctx(workspace_root=tmp_path, context_window_tokens=window),
            provider=provider,  # type: ignore[arg-type]
            history=history,
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=4,
        )
    ]
    return events, history


async def test_size_rejection_compacts_once_and_the_retry_succeeds(tmp_path) -> None:
    provider = _Provider([_TooLongError(), "recovered"])
    events, _history = await _drive(tmp_path, provider, _rows(6), window=100_000)
    assert _text(events) == "recovered"
    assert not any(isinstance(event, Error) for event in events)
    assert any(
        isinstance(event, HarnessIntervention) and event.intervention == "context_overflow_recovery"
        for event in events
    )
    assert provider.summary_calls == 1
    assert provider.main_calls == 2


async def test_size_rejection_with_nothing_to_compact_uses_the_fixed_error(tmp_path) -> None:
    provider = _Provider([_TooLongError()])
    events, _history = await _drive(tmp_path, provider, _rows(2), window=100_000)
    errors = [event for event in events if isinstance(event, Error)]
    assert [event.error for event in errors] == [CONTEXT_OVERFLOW_ERROR]
    assert "prompt is too long" not in errors[0].error
    assert provider.summary_calls == 0
    assert provider.main_calls == 1


async def test_a_second_size_rejection_uses_the_fixed_error(tmp_path) -> None:
    provider = _Provider([_TooLongError(), _TooLongError()])
    events, _history = await _drive(tmp_path, provider, _rows(6), window=100_000)
    errors = [event for event in events if isinstance(event, Error)]
    assert [event.error for event in errors] == [CONTEXT_OVERFLOW_ERROR]
    assert provider.summary_calls == 1
    assert provider.main_calls == 2


async def test_a_tiny_window_the_provider_accepts_still_completes(tmp_path) -> None:
    provider = _Provider(["ok"], counted=10_000)
    events, _history = await _drive(tmp_path, provider, _rows(8, 600), window=800)
    assert _text(events) == "ok"
    assert not any(isinstance(event, Error) for event in events)
    assert provider.summary_calls >= 1
    assert provider.main_calls == 1


def _fixed_errors(events: list[object]) -> list[str]:
    return [event.error for event in events if isinstance(event, Error)]


async def test_size_rejection_with_a_failing_summary_uses_the_fixed_error(tmp_path) -> None:
    provider = _Provider([_TooLongError()], summary_error="runtime")
    rows = _rows(6)
    events, history = await _drive(tmp_path, provider, rows, window=100_000)
    assert _fixed_errors(events) == [CONTEXT_OVERFLOW_ERROR]
    assert "summary failed" not in _fixed_errors(events)[0]
    assert "prompt is too long" not in _fixed_errors(events)[0]
    assert provider.summary_calls == 1
    assert provider.main_calls == 1
    assert history.reset_calls == []


async def test_size_rejection_when_the_summary_is_also_too_long(tmp_path) -> None:
    provider = _Provider([_TooLongError()], summary_error="too_long")
    events, history = await _drive(tmp_path, provider, _rows(6), window=100_000)
    assert _fixed_errors(events) == [CONTEXT_OVERFLOW_ERROR]
    assert "prompt is too long" not in _fixed_errors(events)[0]
    assert provider.summary_calls == 4
    assert provider.main_calls == 1
    assert history.reset_calls == []
