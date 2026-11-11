"""Loop-level retry and idle timeout for a failed or stalled model call."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta, ToolCall
from monkeybot.core.runtime.events import (
    AssistantDelta,
    Error,
    HarnessIntervention,
    SystemNotificationEvent,
    ThinkingBlockComplete,
    TurnComplete,
    UserSteered,
)
from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.provider_retry import (
    RESUME_NOTE,
    Failure,
    RetryPolicy,
    classify,
    idle_timeout_s,
    policy_for,
    recovery,
    use_idle_timeout,
    use_retry_policy,
)
from monkeybot.core.testing.mocks_provider import HangStep, RaiseStep, ScriptedProvider
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.types.interfaces import LLMError
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory


class _RateLimitError(Exception):
    status_code = 429


class _BadRequestError(Exception):
    status_code = 400


class _StatusError(Exception):
    def __init__(self, message: str = "", *, code: int) -> None:
        super().__init__(message)
        self.status_code = code


def _http(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://example.test/v1/messages")
    return httpx.Response(status, headers=headers, request=request)


def _tool(i: int) -> list[object]:
    return [ToolCall(call_id=f"c{i}", name="run_command", args={"i": i}), Done()]


def _unmatched_tool_ids(hist: FakeHistory) -> list[str]:
    open_ids: list[str] = []
    for row in hist.rows:
        for block in row.content:
            if isinstance(block, ToolRequest):
                open_ids.append(block.id)
            elif isinstance(block, ToolResponse) and block.id in open_ids:
                open_ids.remove(block.id)
    return open_ids


def _history_text(hist: FakeHistory) -> str:
    parts: list[str] = []
    for row in hist.rows:
        for block in row.content:
            if isinstance(block, Text):
                parts.append(block.text)
            elif isinstance(block, ToolResponse):
                parts.extend(item.text for item in block.result if isinstance(item, Text))
    return "\n".join(parts)


def _request_text(messages: list[object]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in getattr(message, "content", ()):
            if isinstance(block, Text):
                parts.append(block.text)
            elif isinstance(block, ToolResponse):
                parts.extend(item.text for item in block.result if isinstance(item, Text))
    return "\n".join(parts)


async def _collect(provider: object, hist: FakeHistory, **kwargs: object) -> list[object]:
    events: list[object] = []
    async for evt in run(
        "long task",
        _ctx(),
        provider=provider,  # type: ignore[arg-type]
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
        **kwargs,  # type: ignore[arg-type]
    ):
        events.append(evt)
    return events


def _errors(events: list[object]) -> list[str]:
    return [evt.error for evt in events if isinstance(evt, Error)]


def test_recovery_budgets() -> None:
    policy = RetryPolicy()
    rate_limit = Failure(kind="rate_limit")
    stall = Failure(kind="stall")
    kwargs = {
        "retries_used": 0,
        "stall_retries_used": 0,
        "resumes_used": 0,
        "text_reached_client": False,
    }
    assert recovery(rate_limit, policy, **kwargs) == "retry"  # type: ignore[arg-type]
    assert recovery(rate_limit, policy, **{**kwargs, "retries_used": 5}) == "give_up"
    assert recovery(stall, policy, **kwargs) == "retry"
    assert recovery(stall, policy, **{**kwargs, "stall_retries_used": 1}) == "give_up"
    assert recovery(rate_limit, policy, **{**kwargs, "text_reached_client": True}) == "resume"
    assert (
        recovery(rate_limit, policy, **{**kwargs, "text_reached_client": True, "resumes_used": 2})
        == "give_up"
    )
    assert recovery(Failure(kind="fatal"), policy, **kwargs) == "give_up"  # type: ignore[arg-type]
    assert recovery(rate_limit, RetryPolicy(max_retries=0), **kwargs) == "give_up"


def test_retry_after_is_capped_at_sixty_seconds() -> None:
    policy = RetryPolicy(jitter_s=0)
    failure = Failure(kind="rate_limit", retry_after_s=120)
    assert policy.delay_s(attempt=1, failure=failure) == 60
    assert policy.delay_s(attempt=1, failure=Failure(kind="server")) == 2
    assert policy.delay_s(attempt=5, failure=Failure(kind="server")) == 30
    assert policy.delay_s(attempt=9, failure=Failure(kind="server")) == 30


def test_classify_status_and_sdk_errors() -> None:
    import anthropic
    import openai
    from anthropic._exceptions import OverloadedError
    from google.genai.errors import ClientError, ServerError

    from monkeybot.providers._openai_compat import ProviderRateLimitError, ProviderServerError

    request = httpx.Request("POST", "https://example.test/v1/messages")
    limited = openai.RateLimitError(
        "slow down",
        response=_http(429, {"retry-after": "12"}),
        body={"error": {"message": "slow down"}},
    )
    failure = classify(limited)
    assert failure.kind == "rate_limit"
    assert failure.retry_after_s == 12

    wrapped = ProviderRateLimitError("openai", "m", limited)
    assert classify(wrapped).kind == "rate_limit"
    assert classify(wrapped).retry_after_s == 12

    overloaded = OverloadedError(
        "overloaded",
        response=_http(529),
        body=None,
    )
    assert classify(overloaded).kind == "server"
    assert classify(openai.APITimeoutError(request)).kind == "connection"
    assert classify(anthropic.APIConnectionError(message="reset", request=request)).kind == (
        "connection"
    )

    gemini_limited = ClientError(
        429, {"error": {"message": "exhausted", "status": "RESOURCE_EXHAUSTED"}}
    )
    assert classify(gemini_limited).kind == "rate_limit"
    assert classify(
        ServerError(503, {"error": {"message": "down", "status": "UNAVAILABLE"}})
    ).kind == ("server")
    cause = gemini_limited
    wrapped_gemini = LLMError("429")
    wrapped_gemini.__cause__ = cause
    assert classify(wrapped_gemini).kind == "rate_limit"

    assert classify(ProviderServerError("huggingface", "m", RuntimeError("boom"))).kind == "server"
    assert classify(RuntimeError("boom")).kind == "fatal"
    assert (
        classify(
            openai.BadRequestError(
                "nope",
                response=_http(400),
                body={"error": {"message": "nope"}},
            )
        ).kind
        == "fatal"
    )
    assert classify(_StatusError("timeout", code=408)).kind == "connection"
    assert classify(_StatusError("conflict", code=409)).kind == "connection"
    assert classify(_StatusError("early", code=425)).kind == "connection"
    assert classify(_StatusError("down", code=500)).kind == "server"
    assert classify(ConnectionError("reset")).kind == "connection"
    assert classify(TimeoutError()).kind == "connection"

    when = datetime.now(UTC) + timedelta(seconds=30)
    dated = openai.RateLimitError(
        "later",
        response=_http(429, {"retry-after": format_datetime(when, usegmt=True)}),
        body=None,
    )
    parsed = classify(dated).retry_after_s
    assert parsed is not None and 20 <= parsed <= 40

    milliseconds = openai.RateLimitError(
        "soon",
        response=_http(429, {"retry-after-ms": "1500"}),
        body=None,
    )
    assert classify(milliseconds).retry_after_s == 1.5


def test_yaml_keys_set_the_budget_and_the_idle_timeout(tmp_path: Path) -> None:
    from monkeybot.core.config.runtime_env import ENV_SPEC, ConfigTier
    from monkeybot.core.config.snapshot import build_runtime_config

    path = tmp_path / "monkeybot.yaml"
    path.write_text(
        "model:\n  provider_max_retries: 2\n  stream_idle_timeout_s: 12\n",
        encoding="utf-8",
    )
    cfg = build_runtime_config(config_path=path, agent_root=tmp_path)
    assert cfg.model.provider_max_retries == "2"
    assert cfg.model.stream_idle_timeout_s == "12"
    assert ENV_SPEC["PROVIDER_MAX_RETRIES"] == (ConfigTier.HOT, "model.provider_max_retries")
    assert ENV_SPEC["STREAM_IDLE_TIMEOUT_S"] == (ConfigTier.HOT, "model.stream_idle_timeout_s")
    with use_retry_policy(None):
        assert policy_for(cfg).max_retries == 2
        assert idle_timeout_s(cfg) == 12
        disabled = SimpleNamespace(
            env_values={"PROVIDER_MAX_RETRIES": "0", "STREAM_IDLE_TIMEOUT_S": "0"}
        )
        assert policy_for(disabled).max_retries == 0
        assert idle_timeout_s(disabled) is None


def test_gemini_client_has_a_total_timeout() -> None:
    from monkeybot.providers._sdk_client import aclose_sdk_clients
    from monkeybot.providers.gemini import _GEMINI_HTTP_TIMEOUT_MS, GeminiProvider

    provider = GeminiProvider(api_key="test-key")

    async def _check() -> None:
        client = provider._client("gemini-2.5-flash")
        assert client._api_client._http_options.timeout == _GEMINI_HTTP_TIMEOUT_MS
        await aclose_sdk_clients()

    asyncio.run(_check())


@pytest.mark.asyncio
async def test_rate_limit_on_call_30_retries_and_keeps_the_work() -> None:
    turns: list[list[object]] = [_tool(i) for i in range(1, 30)]
    turns.append([RaiseStep(error=_RateLimitError, message="429 rate limit on inner call 30")])
    turns.append([TextDelta(text="finished"), Done()])
    provider = ScriptedProvider(turns)  # type: ignore[arg-type]
    executor = RecordingExecutor()
    hist = FakeHistory()
    events: list[object] = []
    async for evt in run(
        "long task",
        _ctx(),
        provider=provider,
        history=hist,
        inspectors=[],
        tool_executor=executor,
        max_turns=40,
    ):
        events.append(evt)

    assert provider.stream_calls == 31
    assert len(executor.calls) == 29
    assert _errors(events) == []
    assert isinstance(events[-1], TurnComplete)
    assert _unmatched_tool_ids(hist) == []
    assert provider.stream_messages[-2] == provider.stream_messages[-1]
    assert any(
        isinstance(evt, HarnessIntervention) and evt.intervention == "provider_retry"
        for evt in events
    )
    assert any(
        isinstance(evt, SystemNotificationEvent) and "rate-limited" in evt.msg for evt in events
    )


@pytest.mark.asyncio
async def test_retried_request_is_the_same_bytes() -> None:
    provider = ScriptedProvider(
        [
            [ThinkingDelta(text="planning"), RaiseStep(error=ConnectionError, message="dropped")],
            [TextDelta(text="answer"), Done()],
        ]
    )
    hist = FakeHistory()
    events = await _collect(provider, hist)

    assert provider.stream_calls == 2
    assert provider.stream_messages[0] == provider.stream_messages[1]
    assert _errors(events) == []
    assert _history_text(hist) == "long task\nanswer"
    assert any(isinstance(evt, ThinkingBlockComplete) for evt in events)
    assert any(
        isinstance(evt, HarnessIntervention) and evt.intervention == "provider_retry"
        for evt in events
    )


@pytest.mark.asyncio
async def test_retries_exhausted_end_the_turn_once() -> None:
    class _Always:
        name = "fake"
        supports_streaming = True

        def __init__(self) -> None:
            self.stream_calls = 0

        async def stream(
            self, messages: object, tools: object, *, model: str, **_: object
        ) -> object:
            del messages, tools, model
            self.stream_calls += 1
            raise _RateLimitError("429 rate limit")
            yield  # pragma: no cover

        async def count_input_tokens(
            self, messages: object, tools: object, *, model: str, **_: object
        ) -> int:
            del messages, tools, model
            return 1

    provider = _Always()
    hist = FakeHistory()
    events = await _collect(provider, hist)

    assert provider.stream_calls == 6
    assert _errors(events) == [
        "The model provider is rate-limiting requests. Wait a moment and try again. "
        "(HTTP 429, ref r1)"
    ]
    assert "429 rate limit" not in _errors(events)[0]
    assert isinstance(events[-1], TurnComplete)
    assert _unmatched_tool_ids(hist) == []


@pytest.mark.asyncio
async def test_fatal_errors_are_not_retried() -> None:
    runtime = ScriptedProvider([[RaiseStep(error=RuntimeError, message="boom")]])
    events = await _collect(runtime, FakeHistory())
    assert runtime.stream_calls == 1
    assert _errors(events) == ["The request failed (RuntimeError). (ref r1)"]
    assert "boom" not in _errors(events)[0]

    rejected = ScriptedProvider([[RaiseStep(error=_BadRequestError, message="invalid request")]])
    events = await _collect(rejected, FakeHistory())
    assert rejected.stream_calls == 1
    assert _errors(events) == ["The model provider rejected the request. (HTTP 400, ref r1)"]
    assert "invalid request" not in _errors(events)[0]


@pytest.mark.asyncio
async def test_zero_retries_sends_the_call_once() -> None:
    provider = ScriptedProvider([[RaiseStep(error=_RateLimitError, message="429 rate limit")]])
    hist = FakeHistory()
    with use_retry_policy(None):
        events: list[object] = []
        async for evt in run(
            "long task",
            replace(
                _ctx(),
                config=SimpleNamespace(env_values={"PROVIDER_MAX_RETRIES": "0"}),  # type: ignore[arg-type]
            ),
            provider=provider,
            history=hist,
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=4,
        ):
            events.append(evt)
    assert provider.stream_calls == 1
    assert _errors(events) == [
        "The model provider is rate-limiting requests. Wait a moment and try again. "
        "(HTTP 429, ref r1)"
    ]
    assert "429 rate limit" not in _errors(events)[0]


@pytest.mark.asyncio
async def test_visible_text_and_a_tool_call_are_resumed() -> None:
    provider = ScriptedProvider(
        [
            [
                TextDelta(text="partial answer"),
                ToolCall(call_id="c1", name="run_command", args={"i": 1}),
                RaiseStep(error=ConnectionError, message="dropped"),
            ],
            [TextDelta(text="continued"), Done()],
        ]
    )
    executor = RecordingExecutor()
    hist = FakeHistory()
    events: list[object] = []
    async for evt in run(
        "long task",
        _ctx(),
        provider=provider,
        history=hist,
        inspectors=[],
        tool_executor=executor,
        max_turns=4,
    ):
        events.append(evt)

    assert provider.stream_calls == 2
    assert executor.calls == []
    assert _errors(events) == []
    assert isinstance(events[-1], TurnComplete)
    assert "partial answer" in _history_text(hist)
    assert "interrupted" in _history_text(hist)
    assert "continued" in _history_text(hist)
    assert _unmatched_tool_ids(hist) == []
    assert RESUME_NOTE in _request_text(provider.stream_messages[1])
    assert any(
        isinstance(evt, HarnessIntervention) and evt.intervention == "provider_resume"
        for evt in events
    )
    assert any(isinstance(evt, AssistantDelta) and evt.delta == "continued" for evt in events)


@pytest.mark.asyncio
async def test_a_stalled_stream_is_retried_once() -> None:
    provider = ScriptedProvider([[HangStep(timeout_s=2.0)], [HangStep(timeout_s=2.0)]])
    started = asyncio.get_running_loop().time()
    with use_idle_timeout(0.05):
        events = await _collect(provider, FakeHistory())
    elapsed = asyncio.get_running_loop().time() - started

    assert provider.stream_calls == 2
    assert elapsed < 2
    assert _errors(events) == ["The model provider stopped responding. (ref r1)"]
    assert "stalled" not in _errors(events)[0]
    assert isinstance(events[-1], TurnComplete)
    assert any(
        isinstance(evt, HarnessIntervention) and evt.intervention == "stream_stall"
        for evt in events
    )


@pytest.mark.asyncio
async def test_stop_during_backoff_ends_the_turn() -> None:
    provider = ScriptedProvider(
        [
            [RaiseStep(error=_RateLimitError, message="429 rate limit")],
            [TextDelta(text="should-not-run"), Done()],
        ]
    )
    cancelled = asyncio.Event()

    async def _stop() -> None:
        while provider.stream_calls < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        cancelled.set()

    started = asyncio.get_running_loop().time()
    stopper = asyncio.create_task(_stop())
    with use_retry_policy(RetryPolicy(base_delays_s=(5, 5, 5, 5, 5), jitter_s=0)):
        events = await _collect(provider, FakeHistory(), cancelled=cancelled)
    await stopper
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2
    assert provider.stream_calls == 1
    assert _errors(events) == ["Request cancelled"]


@pytest.mark.asyncio
async def test_steer_during_backoff_preempts() -> None:
    provider = ScriptedProvider(
        [
            [RaiseStep(error=_RateLimitError, message="429 rate limit")],
            [TextDelta(text="after-steer"), Done()],
        ]
    )
    admission = InputAdmission()

    async def _steer() -> None:
        while provider.stream_calls < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        admission.enqueue_steer([Text(text="do this instead")])
        admission.request_preempt()

    steerer = asyncio.create_task(_steer())
    events: list[object] = []
    with use_retry_policy(RetryPolicy(base_delays_s=(5, 5, 5, 5, 5), jitter_s=0)):
        async for evt in run(
            "long task",
            _ctx(),
            provider=provider,
            history=FakeHistory(),
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=4,
            input_admission=admission,
        ):
            events.append(evt)
    await steerer

    assert provider.stream_calls == 2
    assert _errors(events) == []
    steered = [evt for evt in events if isinstance(evt, UserSteered)]
    assert [evt.text for evt in steered] == ["do this instead"]
    assert any(isinstance(evt, AssistantDelta) and evt.delta == "after-steer" for evt in events)
