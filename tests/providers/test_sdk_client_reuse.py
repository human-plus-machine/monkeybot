"""One SDK HTTP client per endpoint, with the model and affinity header per request."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from monkeybot.core.llm.provider import Message, ProviderCallHints
from monkeybot.providers._openai_compat import stream_chat_completions_with_tool_fallback
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.openai import OpenAIProvider
from tests.providers.conftest import make_anthropic_stream_mock


def _openai_chunk() -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                delta=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
    )


def _install_openai(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    constructed: list[Any] = []
    captured: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            captured.append(kwargs)

            async def _stream() -> Any:
                yield _openai_chunk()

            return _stream()

    def _factory(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        constructed.append(client)
        return client

    fake = ModuleType("openai")
    fake.AsyncOpenAI = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return {"constructed": constructed, "captured": captured}


async def _drain_openai(
    provider: OpenAIProvider,
    *,
    model: str,
    hints: ProviderCallHints | None = None,
) -> None:
    async for _event in provider.stream(
        [Message.text("user", "hi")],
        [],
        model=model,
        hints=hints,
    ):
        pass


@pytest.mark.asyncio
async def test_openai_reuses_one_client_across_models_and_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_openai(monkeypatch)
    first = OpenAIProvider()
    second = OpenAIProvider()

    await _drain_openai(
        first,
        model="gpt-4.1",
        hints=ProviderCallHints(session_id="sess-a", cache_retention="short"),
    )
    await _drain_openai(
        second,
        model="gpt-5",
        hints=ProviderCallHints(session_id="sess-b", cache_retention="long"),
    )
    await _drain_openai(
        first,
        model="gpt-4.1",
        hints=ProviderCallHints(session_id="sess-a", cache_retention="none"),
    )

    assert len(seen["constructed"]) == 1
    assert [call["model"] for call in seen["captured"]] == ["gpt-4.1", "gpt-5", "gpt-4.1"]
    assert seen["captured"][0]["extra_headers"] == {"x-session-affinity": "sess-a"}
    assert seen["captured"][1]["extra_headers"] == {"x-session-affinity": "sess-b"}
    assert "extra_headers" not in seen["captured"][2]
    assert seen["captured"][1]["prompt_cache_retention"] == "24h"


def _claude_events() -> list[object]:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
        ),
        SimpleNamespace(
            type="message_delta",
            usage=SimpleNamespace(output_tokens=1),
            delta=SimpleNamespace(stop_reason="end_turn"),
        ),
    ]


@pytest.mark.asyncio
async def test_claude_reuses_one_client_and_sends_affinity_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    constructed: list[Any] = []
    client = make_anthropic_stream_mock(_claude_events())

    async def _count_tokens(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(input_tokens=3)

    client.messages.count_tokens = _count_tokens

    def _factory(**_kwargs: Any) -> Any:
        constructed.append(client)
        return client

    monkeypatch.setattr("anthropic.AsyncAnthropic", _factory)
    provider = ClaudeProvider()
    other = ClaudeProvider()

    await provider.count_input_tokens([], [], model="claude-sonnet-4-5")
    async for _event in provider.stream(
        [Message.text("user", "hi")],
        [],
        model="claude-sonnet-4-5",
        hints=ProviderCallHints(session_id="sess-a"),
    ):
        pass
    async for _event in other.stream(
        [Message.text("user", "hi")],
        [],
        model="claude-opus-4-1",
        hints=ProviderCallHints(session_id="sess-b", cache_retention="none"),
    ):
        pass

    assert len(constructed) == 1
    calls = client.messages.stream.call_args_list
    assert calls[0].kwargs["model"] == "claude-sonnet-4-5"
    assert calls[0].kwargs["extra_headers"] == {"x-session-affinity": "sess-a"}
    assert calls[1].kwargs["model"] == "claude-opus-4-1"
    assert "extra_headers" not in calls[1].kwargs


@pytest.mark.asyncio
async def test_openai_compat_reuses_one_client_per_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_openai(monkeypatch)

    async def _once(model: str) -> None:
        async for _event in stream_chat_completions_with_tool_fallback(
            base_url="https://example.test/v1",
            api_key="compat-key",
            provider="openrouter",
            messages=[Message.text("user", "hi")],
            tools=[],
            model=model,
            temperature=0.0,
            max_tokens=16,
        ):
            pass

    await _once("model-a")
    await _once("model-b")

    assert len(seen["constructed"]) == 1
    assert [call["model"] for call in seen["captured"]] == ["model-a", "model-b"]


def test_gemini_client_follows_endpoint_not_model(monkeypatch: pytest.MonkeyPatch) -> None:
    constructed: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = _Client  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "proj")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    keyed = GeminiProvider(api_key="studio-key")
    assert keyed._client("gemini-2.5-flash") is keyed._client("gemini-2.5-pro")

    vertex = GeminiProvider()
    regional = vertex._client("gemini-2.5-flash")
    assert vertex._client("gemini-2.5-pro") is regional
    global_client = vertex._client("gemini-3-flash-preview")
    assert global_client is not regional
    assert vertex._client("gemini-3-flash-preview") is global_client

    assert constructed == [
        {"api_key": "studio-key"},
        {"vertexai": True, "project": "proj", "location": "us-central1"},
        {"vertexai": True, "project": "proj", "location": "global"},
    ]
