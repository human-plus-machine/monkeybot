"""OpenAI-compatible stream usage parsing: cached-token split and double-count guard."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from monkeybot.core.llm.provider import ThinkingDelta, UsageEvent
from monkeybot.providers._openai_compat import (
    ProviderRateLimitError,
    ProviderServerError,
    is_rate_limit_error,
    is_server_error,
    iter_openai_compat_stream,
    stream_chat_completions_with_tool_fallback,
)


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


def _text_chunk(content: str = "hi", *, reasoning: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning=reasoning, tool_calls=None)
            )
        ],
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


def _flaky_client(exc: Exception, *, fail_times: int, chunks: list[SimpleNamespace]) -> Any:
    """Client whose ``create`` raises *exc* the first ``fail_times`` calls, then streams."""
    calls = {"n": 0}

    async def _create(**_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc

        async def _stream() -> Any:
            for chunk in chunks:
                yield chunk

        return _stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    client.calls = calls
    return client


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
async def test_iter_openai_compat_stream_yields_reasoning_delta() -> None:
    client = _fake_client([_text_chunk(reasoning="plan step")])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    thinking = [ev for ev in events if isinstance(ev, ThinkingDelta)]
    assert len(thinking) == 1
    assert thinking[0].text == "plan step"


@pytest.mark.asyncio
async def test_iter_openai_compat_stream_reads_thinking_from_model_extra() -> None:
    delta = SimpleNamespace(
        content=None,
        reasoning=None,
        tool_calls=None,
        model_extra={"thinking": "hidden plan"},
    )
    chunk = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=delta)],
    )
    client = _fake_client([chunk])
    events = [ev async for ev in iter_openai_compat_stream(client, {})]
    thinking = [ev for ev in events if isinstance(ev, ThinkingDelta)]
    assert len(thinking) == 1
    assert thinking[0].text == "hidden plan"


@pytest.mark.asyncio
async def test_stream_chat_completions_forwards_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)

        async def _stream() -> Any:
            if False:  # pragma: no cover
                yield

        return _stream()

    def _fake_openai(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
        )

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = _fake_openai
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    _ = [
        ev
        async for ev in stream_chat_completions_with_tool_fallback(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            provider="ollama",
            messages=[],
            tools=[],
            model="gemma4",
            temperature=0.7,
            max_tokens=100,
            reasoning_effort="none",
        )
    ]
    assert captured[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_iter_openai_compat_stream_sets_include_usage() -> None:
    captured: list[dict[str, Any]] = []

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)

        async def _stream() -> Any:
            if False:  # pragma: no cover — make this an async generator
                yield

        return _stream()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
    )
    _ = [ev async for ev in iter_openai_compat_stream(client, {"model": "m", "stream": True})]
    assert captured[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_iter_openai_compat_stream_preserves_explicit_stream_options() -> None:
    captured: list[dict[str, Any]] = []

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)

        async def _stream() -> Any:
            if False:  # pragma: no cover
                yield

        return _stream()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
    )
    custom = {"include_usage": False}
    _ = [
        ev
        async for ev in iter_openai_compat_stream(
            client,
            {"model": "m", "stream": True, "stream_options": custom},
        )
    ]
    assert captured[0]["stream_options"] is custom


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


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda *_a, **_kw: client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)


# NVIDIA's own quota text — arrives as a plain exception message, not a clean
# HTTP 429, on integrate.api.nvidia.com's free tier.
_NVIDIA_RESOURCE_EXHAUSTED = RuntimeError(
    "ResourceExhausted: Worker local total request limit reached (17/16)"
)


@pytest.mark.asyncio
async def test_rate_limit_retries_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate-limit error before any chunk is streamed retries and eventually succeeds."""
    client = _flaky_client(
        _NVIDIA_RESOURCE_EXHAUSTED, fail_times=1, chunks=[_text_chunk("hi")]
    )
    _install_fake_openai(monkeypatch, client)

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep
    )

    events = [
        ev
        async for ev in stream_chat_completions_with_tool_fallback(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test",
            provider="nvidia",
            messages=[],
            tools=[],
            model="meta/llama-3.3-70b-instruct",
            temperature=0.7,
            max_tokens=100,
        )
    ]

    assert client.calls["n"] == 2  # first attempt failed, second succeeded
    assert len(sleeps) == 1
    assert any(isinstance(ev, UsageEvent) for ev in events)


@pytest.mark.asyncio
async def test_rate_limit_exhausted_raises_friendly_error_not_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After retries are exhausted, the raw NVIDIA counter string never reaches callers."""
    client = _failing_client(_NVIDIA_RESOURCE_EXHAUSTED)
    _install_fake_openai(monkeypatch, client)

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        [
            ev
            async for ev in stream_chat_completions_with_tool_fallback(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key="nvapi-test",
                provider="nvidia",
                messages=[],
                tools=[],
                model="meta/llama-3.3-70b-instruct",
                temperature=0.7,
                max_tokens=100,
            )
        ]

    surfaced = str(exc_info.value)
    assert "Worker local total request limit" not in surfaced
    assert "17/16" not in surfaced
    assert "nvidia" in surfaced.lower()
    # Raw upstream text is preserved for logs, just not surfaced to the user.
    assert "Worker local total request limit" in str(exc_info.value.original)


@pytest.mark.asyncio
async def test_rate_limit_backoff_sleeps_after_releasing_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concurrency slot must be released before backing off, not held idle."""
    client = _flaky_client(
        _NVIDIA_RESOURCE_EXHAUSTED, fail_times=1, chunks=[_text_chunk("hi")]
    )
    _install_fake_openai(monkeypatch, client)

    events: list[str] = []

    class _SpySemaphore:
        async def __aenter__(self) -> None:
            events.append("acquire")

        async def __aexit__(self, *_exc: object) -> None:
            events.append("release")

    monkeypatch.setattr(
        "monkeybot.providers._openai_compat._provider_semaphore",
        lambda _provider: _SpySemaphore(),
    )

    async def _fake_sleep(_delay: float) -> None:
        events.append("sleep")

    monkeypatch.setattr(
        "monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep
    )

    _ = [
        ev
        async for ev in stream_chat_completions_with_tool_fallback(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test",
            provider="nvidia",
            messages=[],
            tools=[],
            model="meta/llama-3.3-70b-instruct",
            temperature=0.7,
            max_tokens=100,
        )
    ]

    # release must precede sleep — a backing-off request cannot hold the slot idle.
    assert events == ["acquire", "release", "sleep", "acquire", "release"]


@pytest.mark.asyncio
async def test_mid_stream_rate_limit_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once partial output has streamed, retrying would duplicate it — must not retry."""

    async def _create(**_kwargs: Any) -> Any:
        async def _stream() -> Any:
            yield _text_chunk("partial")
            raise _NVIDIA_RESOURCE_EXHAUSTED

        return _stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    _install_fake_openai(monkeypatch, client)

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep
    )

    with pytest.raises(ProviderRateLimitError):
        [
            ev
            async for ev in stream_chat_completions_with_tool_fallback(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key="nvapi-test",
                provider="nvidia",
                messages=[],
                tools=[],
                model="meta/llama-3.3-70b-instruct",
                temperature=0.7,
                max_tokens=100,
            )
        ]

    assert sleeps == []  # no retry attempted once output had already streamed


def test_is_rate_limit_error_classifies_real_openai_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RateLimitError and a 429 APIStatusError are recognized, a 500 is not."""

    class FakeRateLimitError(Exception):
        pass

    class FakeAPIStatusError(Exception):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"status {status_code}")
            self.status_code = status_code

    fake_openai = ModuleType("openai")
    fake_openai.RateLimitError = FakeRateLimitError
    fake_openai.APIStatusError = FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    assert is_rate_limit_error(FakeRateLimitError("too many requests"))
    assert is_rate_limit_error(FakeAPIStatusError(429))
    assert not is_rate_limit_error(FakeAPIStatusError(500))


class _FakeAPIError(Exception):
    """Mirrors real openai.APIError: base class, no status_code attribute."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _FakeAPIStatusError(_FakeAPIError):
    """Mirrors real openai.APIStatusError: subclass of APIError, has status_code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _install_fake_openai_with_status_error(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda *_a, **_kw: client
    fake_openai.APIError = _FakeAPIError
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)


def test_is_server_error_classifies_5xx_not_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5xx APIStatusError is a server error; a 4xx (or non-status exception) is not."""
    fake_openai = ModuleType("openai")
    fake_openai.APIError = _FakeAPIError
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    assert is_server_error(_FakeAPIStatusError(500))
    assert is_server_error(_FakeAPIStatusError(503))
    assert not is_server_error(_FakeAPIStatusError(429))
    assert not is_server_error(_FakeAPIStatusError(400))
    assert not is_server_error(RuntimeError("Internal server error"))


def test_is_server_error_classifies_mid_stream_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare APIError (no status_code) with 'Internal server error' text is recognized.

    Reproduces exactly what NVIDIA's nemotron endpoint does live: the initial
    HTTP response is 200 OK, then an error chunk arrives mid-stream and the
    OpenAI SDK raises the *base* APIError class (openai/_streaming.py
    __stream__), not APIStatusError — this has no status_code attribute at
    all, so the APIStatusError-only check misses it entirely.
    """
    fake_openai = ModuleType("openai")
    fake_openai.APIError = _FakeAPIError
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    assert is_server_error(_FakeAPIError("Internal server error"))
    assert not is_server_error(_FakeAPIError("Invalid request: missing required field"))


@pytest.mark.asyncio
async def test_server_error_retries_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare 5xx before any chunk is streamed retries and eventually succeeds."""
    client = _flaky_client(
        _FakeAPIStatusError(500), fail_times=1, chunks=[_text_chunk("hi")]
    )
    _install_fake_openai_with_status_error(monkeypatch, client)

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep)

    events = [
        ev
        async for ev in stream_chat_completions_with_tool_fallback(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test",
            provider="nvidia",
            messages=[],
            tools=[],
            model="meta/llama-3.3-70b-instruct",
            temperature=0.7,
            max_tokens=100,
        )
    ]

    assert client.calls["n"] == 2  # first attempt failed, second succeeded
    assert len(sleeps) == 1
    assert any(isinstance(ev, UsageEvent) for ev in events)


@pytest.mark.asyncio
async def test_server_error_exhausted_raises_provider_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After retries are exhausted, a bare 'Internal server error' never reaches callers raw."""
    client = _failing_client(_FakeAPIStatusError(500))
    _install_fake_openai_with_status_error(monkeypatch, client)

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep)

    with pytest.raises(ProviderServerError) as exc_info:
        [
            ev
            async for ev in stream_chat_completions_with_tool_fallback(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key="nvapi-test",
                provider="nvidia",
                messages=[],
                tools=[],
                model="meta/llama-3.3-70b-instruct",
                temperature=0.7,
                max_tokens=100,
            )
        ]

    assert "nvidia" in str(exc_info.value).lower()
    assert "status 500" in str(exc_info.value.original)


@pytest.mark.asyncio
async def test_mid_stream_server_error_surfaces_friendly_message_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the exact live failure: NVIDIA returns 200 OK, streams a
    thinking/text chunk, then a bare APIError('Internal server error') mid-stream.
    Partial output already streamed means it must not retry (would duplicate
    output) — but the raw upstream text still must not reach the caller raw.
    """

    async def _create(**_kwargs: Any) -> Any:
        async def _stream() -> Any:
            yield _text_chunk("Searching Dice for")
            raise _FakeAPIError("Internal server error")

        return _stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    _install_fake_openai_with_status_error(monkeypatch, client)

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("monkeybot.providers._openai_compat.asyncio.sleep", _fake_sleep)

    with pytest.raises(ProviderServerError) as exc_info:
        [
            ev
            async for ev in stream_chat_completions_with_tool_fallback(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key="nvapi-test",
                provider="nvidia",
                messages=[],
                tools=[],
                model="nvidia/nemotron-3-ultra-550b-a55b",
                temperature=0.7,
                max_tokens=100,
            )
        ]

    assert "Internal server error" not in str(exc_info.value)
    assert "nvidia" in str(exc_info.value).lower()
    assert "Internal server error" in str(exc_info.value.original)


def _tool_call_text_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, reasoning=None, tool_calls=None)
            )
        ],
    )


@pytest.mark.asyncio
async def test_recovers_literal_tool_call_text_when_no_structured_call() -> None:
    """A Hermes-style literal <tool_call> block is recovered as a real ToolCall.

    Some NIM/vLLM-hosted models (e.g. NVIDIA's nemotron line) emit a call as
    inline text instead of populating the structured tool_calls delta when the
    server's chat template isn't wired for native tool-calling. Without
    recovery, the tool never runs and the raw XML leaks into the chat as text.
    """
    client = _fake_client(
        [
            _tool_call_text_chunk(
                '<tool_call>\n{"name": "write_file", '
                '"arguments": {"path": "pipeline/jobs.json", "content": "{}"}}\n</tool_call>'
            )
        ]
    )

    events = [
        ev
        async for ev in iter_openai_compat_stream(
            client,
            {"model": "m", "tools": [{"type": "function", "function": {"name": "write_file"}}]},
            provider="nvidia",
            n_tools=1,
        )
    ]

    from monkeybot.core.llm.provider import ToolCall

    tool_calls = [ev for ev in events if isinstance(ev, ToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "write_file"
    assert tool_calls[0].args == {"path": "pipeline/jobs.json", "content": "{}"}


@pytest.mark.asyncio
async def test_does_not_recover_text_tool_call_when_structured_call_present() -> None:
    """A real structured tool call must never be overridden/duplicated by text recovery."""

    def _structured_tool_call_chunk() -> SimpleNamespace:
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="search_jobs", arguments='{"keyword": "python"}'
                                ),
                            )
                        ],
                    )
                )
            ],
        )

    client = _fake_client([_structured_tool_call_chunk()])

    from monkeybot.core.llm.provider import ToolCall

    events = [
        ev
        async for ev in iter_openai_compat_stream(
            client,
            {"model": "m", "tools": [{"type": "function", "function": {"name": "search_jobs"}}]},
            provider="nvidia",
            n_tools=1,
        )
    ]
    tool_calls = [ev for ev in events if isinstance(ev, ToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "search_jobs"
