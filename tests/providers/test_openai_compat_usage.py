"""OpenAI-compatible stream usage parsing: cached-token split and double-count guard."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from monkeybot.core.llm.provider import UsageEvent
from monkeybot.providers._openai_compat import iter_openai_compat_stream


def _usage_chunk(
    prompt: int,
    completion: int,
    *,
    cached: int | None = None,
    omit_details: bool = False,
    details_without_cached: bool = False,
) -> SimpleNamespace:
    if omit_details:
        usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    elif details_without_cached:
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(),
        )
    else:
        details = SimpleNamespace(cached_tokens=cached)
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=details,
        )
    return SimpleNamespace(usage=usage, choices=[])


def _text_chunk(content: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))],
    )


def _fake_client(chunks: list[SimpleNamespace]) -> Any:
    async def _stream() -> Any:
        for chunk in chunks:
            yield chunk

    async def _create(**_kwargs: Any) -> Any:
        return _stream()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def _failing_client(exc: Exception) -> Any:
    async def _create(**_kwargs: Any) -> Any:
        raise exc

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def _usage_from_events(events: list[Any]) -> UsageEvent:
    return next(e for e in events if isinstance(e, UsageEvent))


@pytest.mark.asyncio
async def test_usage_event_splits_cached_tokens() -> None:
    client = _fake_client([_usage_chunk(prompt=1000, completion=50, cached=900)])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_tokens == 900
    assert usage.cache_creation_tokens == 0
    assert usage.cached_tokens == 900


@pytest.mark.asyncio
async def test_usage_event_no_prompt_tokens_details() -> None:
    client = _fake_client([_usage_chunk(prompt=1000, completion=10, omit_details=True)])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 10
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cached_tokens == 0


@pytest.mark.asyncio
async def test_usage_event_details_present_but_no_cached_tokens() -> None:
    client = _fake_client([_usage_chunk(prompt=500, completion=0, details_without_cached=True)])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.input_tokens == 500
    assert usage.cached_tokens == 0
    assert usage.cache_read_tokens == 0


@pytest.mark.asyncio
async def test_usage_event_cached_tokens_none() -> None:
    client = _fake_client([_usage_chunk(prompt=200, completion=0, cached=None)])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.input_tokens == 200
    assert usage.cached_tokens == 0


@pytest.mark.asyncio
async def test_usage_event_no_usage_chunk() -> None:
    client = _fake_client([_text_chunk()])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cached_tokens == 0
    assert usage.cache_read_tokens == 0


@pytest.mark.asyncio
async def test_usage_event_zero_cached() -> None:
    client = _fake_client([_usage_chunk(prompt=300, completion=0, cached=0)])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.input_tokens == 300
    assert usage.cached_tokens == 0
    assert usage.cache_read_tokens == 0


@pytest.mark.asyncio
async def test_invariant_cached_equals_read_plus_creation() -> None:
    client = _fake_client([_usage_chunk(prompt=1000, completion=0, cached=900)])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    usage = _usage_from_events(events)
    assert usage.cached_tokens == 900
    assert usage.cache_read_tokens == 900
    assert usage.cache_creation_tokens == 0
    assert usage.cached_tokens == usage.cache_read_tokens + usage.cache_creation_tokens


@pytest.mark.asyncio
async def test_stream_error_logs_structured_context(caplog: pytest.LogCaptureFixture) -> None:
    client = _failing_client(RuntimeError("boom"))
    with (
        caplog.at_level("WARNING", logger="monkeybot.providers._openai_compat"),
        pytest.raises(RuntimeError, match="boom"),
    ):
        [
            ev
            async for ev in iter_openai_compat_stream(
                client,
                {"model": "m"},
                provider="openai",
                n_messages=2,
                n_tools=3,
            )
        ]

    assert "OpenAI-compat stream error" in caplog.text
    assert "provider=openai" in caplog.text
    assert "model=m" in caplog.text
    assert "n_messages=2" in caplog.text
    assert "n_tools=3" in caplog.text
