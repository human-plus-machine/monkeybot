"""Tests for provider stream retry/backoff."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest

from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta
from monkeybot.core.llm.retry import (
    ProviderRetryConfig,
    compute_retry_delay_seconds,
    is_transient_provider_error,
    retrying_provider_stream,
)
from monkeybot.core.runtime.events import AssistantDelta, Error, TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.types_tools import ToolDef


class _FakeProvider:
    def __init__(self, behaviors: list[object]) -> None:
        self._behaviors = list(behaviors)
        self.attempts = 0

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
        del messages, tools, model, thinking_budget
        self.attempts += 1
        behavior = self._behaviors[self.attempts - 1]
        if isinstance(behavior, BaseException):
            raise behavior
        for event in behavior:
            yield event

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> int:
        del messages, tools, model
        return 0


def _status_error(status: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/messages")
    response = httpx.Response(status, request=request, headers={"retry-after": "2"})
    return httpx.HTTPStatusError(message, request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(429, "rate limited"), True),
        (_status_error(503, "unavailable"), True),
        (_status_error(529, "overloaded"), True),
        (_status_error(401, "unauthorized"), False),
        (_status_error(400, "bad request"), False),
        (TimeoutError("timed out"), True),
        (ConnectionError("connection refused"), True),
        (ConnectionResetError("connection reset"), True),
        (PermissionError("permission denied"), False),
        (RuntimeError("rate limit exceeded"), True),
        (RuntimeError("invalid api key"), False),
    ],
)
def test_is_transient_provider_error(exc: BaseException, expected: bool) -> None:
    assert is_transient_provider_error(exc) is expected


def test_compute_retry_delay_honors_retry_after() -> None:
    exc = _status_error(429, "rate limited")
    cfg = ProviderRetryConfig(base_delay_s=1.0, max_delay_s=60.0, jitter_fraction=0.0)
    assert compute_retry_delay_seconds(0, exc, cfg) == 2.0


def test_compute_retry_delay_exponential_without_header() -> None:
    exc = RuntimeError("server error")
    cfg = ProviderRetryConfig(base_delay_s=1.0, max_delay_s=60.0, jitter_fraction=0.0)
    assert compute_retry_delay_seconds(0, exc, cfg) == 1.0
    assert compute_retry_delay_seconds(2, exc, cfg) == 4.0


def test_compute_retry_delay_jitter_respects_max_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("monkeybot.core.llm.retry.random.random", lambda: 1.0)
    exc = RuntimeError("server error")
    cfg = ProviderRetryConfig(base_delay_s=1.0, max_delay_s=60.0, jitter_fraction=0.25)
    assert compute_retry_delay_seconds(10, exc, cfg) == 60.0


@pytest.mark.asyncio
async def test_retrying_provider_stream_recovers_before_first_event() -> None:
    provider = _FakeProvider(
        [
            _status_error(429, "rate limited"),
            [TextDelta(text="hello"), Done()],
        ]
    )
    cfg = ProviderRetryConfig(max_attempts=3, base_delay_s=0.0, jitter_fraction=0.0)

    events: list[ProviderEvent] = []
    async for event in retrying_provider_stream(
        provider,
        [Message.text("user", "hi")],
        [],
        model="test-model",
        config=cfg,
    ):
        events.append(event)

    assert provider.attempts == 2
    assert events == [TextDelta(text="hello"), Done()]


@pytest.mark.asyncio
async def test_retrying_provider_stream_does_not_retry_after_partial_emit() -> None:
    class PartialFailProvider:
        @property
        def name(self) -> str:
            return "partial"

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
            yield TextDelta(text="partial")
            raise _status_error(503, "mid-stream failure")

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
        ) -> int:
            del messages, tools, model
            return 0

    cfg = ProviderRetryConfig(max_attempts=4, base_delay_s=0.0, jitter_fraction=0.0)
    events: list[ProviderEvent] = []
    with pytest.raises(httpx.HTTPStatusError):
        async for event in retrying_provider_stream(
            PartialFailProvider(),
            [Message.text("user", "hi")],
            [],
            model="test-model",
            config=cfg,
        ):
            events.append(event)

    assert events == [TextDelta(text="partial")]


@pytest.mark.asyncio
async def test_retrying_provider_stream_does_not_retry_permanent_error() -> None:
    provider = _FakeProvider([_status_error(401, "unauthorized")])
    cfg = ProviderRetryConfig(max_attempts=4, base_delay_s=0.0, jitter_fraction=0.0)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in retrying_provider_stream(
            provider,
            [Message.text("user", "hi")],
            [],
            model="test-model",
            config=cfg,
        ):
            pass

    assert provider.attempts == 1


@pytest.mark.asyncio
async def test_run_uses_retry_and_recovers_from_transient_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.core.test_loop import FakeHistory, RecordingExecutor, _ctx

    monkeypatch.setenv("MONKEYBOT_PROVIDER_RETRY_BASE_DELAY_SEC", "0")
    monkeypatch.setenv("MONKEYBOT_PROVIDER_RETRY_JITTER_FRACTION", "0")

    class RetryThenOkProvider:
        def __init__(self) -> None:
            self.attempts = 0

        @property
        def name(self) -> str:
            return "retry-then-ok"

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
            self.attempts += 1
            if self.attempts == 1:
                raise _status_error(429, "rate limited")
            yield TextDelta(text="recovered")
            yield Done()

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
        ) -> int:
            del messages, tools, model
            return 0

    provider = RetryThenOkProvider()
    events: list[Any] = []
    async for event in run(
        "hello",
        _ctx(),
        provider=provider,
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        events.append(event)

    assert provider.attempts == 2
    assert any(isinstance(e, AssistantDelta) and e.delta == "recovered" for e in events)
    assert not any(isinstance(e, Error) for e in events)
    assert isinstance(events[-1], TurnComplete)
