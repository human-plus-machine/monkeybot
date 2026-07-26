"""Sonnet-5-class models: omit rejected sampling/thinking params, retry-and-strip fallback."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from monkeybot.core.llm.provider import Message, UsageEvent
from monkeybot.providers._utils import iter_anthropic_sdk_stream
from monkeybot.providers.claude import ClaudeProvider
from tests.providers.conftest import CANONICAL_TOOL_DEFS, make_anthropic_stream_mock

_SONNET_5 = "claude-sonnet-5-20260101"
_OLDER = "claude-3-5-sonnet-20241022"


def _messages() -> list[Message]:
    return [Message.text("user", "hi")]


def _minimal_events() -> list[object]:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
        ),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=1)),
    ]


async def _drain(
    provider: ClaudeProvider, *, model: str, thinking_budget: int | None = None
) -> None:
    async for _ in provider.stream(
        _messages(), CANONICAL_TOOL_DEFS, model=model, thinking_budget=thinking_budget
    ):
        pass


# --- static capability table wired into claude.py ---


@pytest.mark.asyncio
async def test_sonnet5_omits_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client = make_anthropic_stream_mock(_minimal_events())
    provider = ClaudeProvider(temperature=0.9)
    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _drain(provider, model=_SONNET_5)
    kwargs = client.messages.stream.call_args.kwargs
    assert "temperature" not in kwargs


@pytest.mark.asyncio
async def test_older_model_still_sends_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client = make_anthropic_stream_mock(_minimal_events())
    provider = ClaudeProvider(temperature=0.9)
    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _drain(provider, model=_OLDER)
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["temperature"] == 0.9


@pytest.mark.asyncio
async def test_sonnet5_drops_manual_thinking_and_does_not_force_temperature_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client = make_anthropic_stream_mock(_minimal_events())
    provider = ClaudeProvider(temperature=0.9)
    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _drain(provider, model=_SONNET_5, thinking_budget=4000)
    kwargs = client.messages.stream.call_args.kwargs
    assert "thinking" not in kwargs
    assert "temperature" not in kwargs


@pytest.mark.asyncio
async def test_older_model_manual_thinking_forces_temperature_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client = make_anthropic_stream_mock(_minimal_events())
    provider = ClaudeProvider(temperature=0.9)
    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _drain(provider, model=_OLDER, thinking_budget=4000)
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert kwargs["temperature"] == 1


# --- retry-and-strip fallback for a model not yet in the static table ---


class _FailThenSucceedStream:
    """First open raises a 400-like error naming a rejected param; second succeeds."""

    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        attempt = len(self.calls)
        return self._cm(attempt)

    def _cm(self, attempt: int) -> Any:
        events = self._events

        class _CM:
            async def __aenter__(self) -> object:
                if attempt == 1:
                    raise RuntimeError(
                        "Error code: 400 - temperature: Extra inputs are not permitted"
                    )

                async def _gen() -> object:
                    for event in events:
                        yield event

                return _gen()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _CM()


def _cm_raising(exc: Exception) -> Any:
    class _CM:
        async def __aenter__(self) -> object:
            raise exc

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    return _CM()


@pytest.mark.asyncio
async def test_retries_without_rejected_param_on_unlisted_model() -> None:
    stream_fn = _FailThenSucceedStream(_minimal_events())
    client = MagicMock()
    client.messages.stream = stream_fn

    events: list[object] = []
    async for event in iter_anthropic_sdk_stream(
        client,
        {"model": "claude-sonnet-6-hypothetical", "temperature": 0.7, "max_tokens": 100},
        provider="claude",
        error_message="Claude stream error: %s",
    ):
        events.append(event)

    assert len(stream_fn.calls) == 2
    assert "temperature" not in stream_fn.calls[1]
    assert any(isinstance(e, UsageEvent) for e in events)


@pytest.mark.asyncio
async def test_does_not_retry_when_error_names_no_known_param() -> None:
    class _AlwaysFails(_FailThenSucceedStream):
        def _cm(self, attempt: int) -> Any:
            class _CM:
                async def __aenter__(self) -> object:
                    raise RuntimeError("Error code: 500 - internal server error")

                async def __aexit__(self, *_exc: object) -> bool:
                    return False

            return _CM()

    stream_fn = _AlwaysFails(_minimal_events())
    client = MagicMock()
    client.messages.stream = stream_fn

    with pytest.raises(RuntimeError):
        async for _ in iter_anthropic_sdk_stream(
            client,
            {"model": "claude-sonnet-6-hypothetical", "temperature": 0.7, "max_tokens": 100},
            provider="claude",
            error_message="Claude stream error: %s",
        ):
            pass

    assert len(stream_fn.calls) == 1


@pytest.mark.asyncio
async def test_does_not_strip_thinking_on_message_shaping_400() -> None:
    """A 400 that merely mentions thinking must not silently disable thinking."""

    calls: list[dict[str, Any]] = []

    def stream_fn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _cm_raising(
            RuntimeError(
                "Error code: 400 - messages.1.content.0.type: Expected 'thinking' or "
                "'redacted_thinking', but found 'text'. When 'thinking' is enabled, a "
                "final 'assistant' message must start with a thinking block."
            )
        )

    client = MagicMock()
    client.messages.stream = stream_fn

    with pytest.raises(RuntimeError):
        async for _ in iter_anthropic_sdk_stream(
            client,
            {
                "model": "claude-sonnet-6-hypothetical",
                "thinking": {"type": "enabled", "budget_tokens": 4000},
                "temperature": 1,
                "max_tokens": 100,
            },
            provider="claude",
            error_message="Claude stream error: %s",
        ):
            pass

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_strips_multiple_rejected_params_across_retries() -> None:
    """The API blames one field at a time; keep dropping until the request is accepted."""

    calls: list[dict[str, Any]] = []

    def stream_fn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if "thinking" in kwargs:
            return _cm_raising(
                RuntimeError("Error code: 400 - thinking.budget_tokens: unsupported")
            )
        if "temperature" in kwargs:
            return _cm_raising(
                RuntimeError("Error code: 400 - temperature: Extra inputs are not permitted")
            )

        class _CM:
            async def __aenter__(self) -> object:
                async def _gen() -> object:
                    for event in _minimal_events():
                        yield event

                return _gen()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _CM()

    client = MagicMock()
    client.messages.stream = stream_fn

    events: list[object] = []
    async for event in iter_anthropic_sdk_stream(
        client,
        {
            "model": "claude-sonnet-6-hypothetical",
            "thinking": {"type": "enabled", "budget_tokens": 4000},
            "temperature": 1,
            "max_tokens": 100,
        },
        provider="claude",
        error_message="Claude stream error: %s",
    ):
        events.append(event)

    assert len(calls) == 3
    assert "thinking" not in calls[2]
    assert "temperature" not in calls[2]
    assert any(isinstance(e, UsageEvent) for e in events)


@pytest.mark.asyncio
async def test_does_not_retry_after_content_started() -> None:
    """Once deltas have been yielded, a failure must propagate — no duplicated output."""

    calls: list[dict[str, Any]] = []

    def stream_fn(**kwargs: Any) -> Any:
        calls.append(kwargs)

        class _CM:
            async def __aenter__(self) -> object:
                async def _gen() -> object:
                    yield SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text="partial"),
                    )
                    raise RuntimeError(
                        "Error code: 400 - temperature: Extra inputs are not permitted"
                    )

                return _gen()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _CM()

    client = MagicMock()
    client.messages.stream = stream_fn

    with pytest.raises(RuntimeError):
        async for _ in iter_anthropic_sdk_stream(
            client,
            {"model": "claude-sonnet-6-hypothetical", "temperature": 0.7, "max_tokens": 100},
            provider="claude",
            error_message="Claude stream error: %s",
        ):
            pass

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_does_not_mutate_caller_kwargs() -> None:
    stream_fn = _FailThenSucceedStream(_minimal_events())
    client = MagicMock()
    client.messages.stream = stream_fn

    kwargs = {"model": "claude-sonnet-6-hypothetical", "temperature": 0.7, "max_tokens": 100}
    async for _ in iter_anthropic_sdk_stream(
        client, kwargs, provider="claude", error_message="Claude stream error: %s"
    ):
        pass

    assert kwargs["temperature"] == 0.7
