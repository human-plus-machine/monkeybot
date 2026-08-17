"""Gemini usage telemetry."""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any

import pytest

from monkeybot.core.llm.provider import GroundingEvent, Message, UsageEvent
from monkeybot.core.types.interfaces import LLMError
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers.gemini import (
    GeminiProvider,
    _estimate_developer_api_tool_tokens,
    _grounding_metadata_to_dict,
    _usage_from_response,
)


def test_usage_maps_cached_count_to_split_fields() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=1200,
        candidates_token_count=80,
        cached_content_token_count=500,
    )
    ev = _usage_from_response(um)
    assert ev == UsageEvent(
        input_tokens=1200,
        output_tokens=80,
        cached_tokens=500,
        cache_read_tokens=500,
        cache_creation_tokens=0,
    )


def test_usage_no_cached_count_zeroes_cache_fields() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=300,
        candidates_token_count=40,
    )
    ev = _usage_from_response(um)
    assert ev == UsageEvent(
        input_tokens=300,
        output_tokens=40,
        cached_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


def test_usage_cached_count_none_coerces_to_zero() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=300,
        candidates_token_count=40,
        cached_content_token_count=None,
    )
    ev = _usage_from_response(um)
    assert ev is not None
    assert ev.cached_tokens == 0
    assert ev.cache_read_tokens == 0
    assert ev.cache_creation_tokens == 0


def test_usage_invariant_total_equals_read_plus_creation() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=1200,
        candidates_token_count=80,
        cached_content_token_count=500,
    )
    ev = _usage_from_response(um)
    assert ev is not None
    assert ev.cached_tokens == ev.cache_read_tokens + ev.cache_creation_tokens


def test_usage_none_metadata_returns_none() -> None:
    assert _usage_from_response(None) is None


def test_grounding_metadata_to_dict_none_returns_none() -> None:
    assert _grounding_metadata_to_dict(None) is None


def test_grounding_metadata_to_dict_empty_returns_none() -> None:
    gm = types.SimpleNamespace(grounding_chunks=[], web_search_queries=[])
    assert _grounding_metadata_to_dict(gm) is None


def test_grounding_metadata_to_dict_extracts_sources_and_queries() -> None:
    chunk = types.SimpleNamespace(web=types.SimpleNamespace(title="Example", uri="https://example.com"))
    gm = types.SimpleNamespace(
        grounding_chunks=[chunk],
        web_search_queries=["weather today"],
    )
    out = _grounding_metadata_to_dict(gm)
    assert out == {
        "sources": [{"title": "Example", "uri": "https://example.com"}],
        "search_queries": ["weather today"],
    }


def _install_fake_google_genai(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_chunks: list[Any] | None = None,
    count_tokens_error: BaseException | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeModels:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            captured["config"] = kwargs["config"]

            async def _gen() -> AsyncIterator[Any]:
                if stream_chunks is None:
                    return
                    yield  # pragma: no cover
                for chunk in stream_chunks:
                    yield chunk

            return _gen()

        async def count_tokens(self, **kwargs: Any) -> Any:
            if count_tokens_error is not None:
                raise count_tokens_error
            captured["model"] = kwargs["model"]
            captured["contents"] = kwargs["contents"]
            captured["config"] = kwargs.get("config")
            return types.SimpleNamespace(total_tokens=42)

    class FakeClient:
        def __init__(self, **client_kwargs: Any) -> None:
            captured["client_kwargs"] = client_kwargs
            self.aio = types.SimpleNamespace(models=FakeModels())

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_types = ModuleType("google.genai.types")
    fake_genai.Client = FakeClient  # type: ignore[attr-defined]
    fake_types.GenerateContentConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.CountTokensConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.GenerationConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.ThinkingConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.Tool = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.GoogleSearch = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.FunctionDeclaration = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.Content = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.Part = lambda **kw: kw  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    fake_genai.types = fake_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "p")
    return captured


@pytest.mark.asyncio
async def test_stream_adds_google_search_tool_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    cand = types.SimpleNamespace(
        content=types.SimpleNamespace(parts=[types.SimpleNamespace(text="hi", function_call=None)]),
        grounding_metadata=None,
    )
    captured_config = _install_fake_google_genai(
        monkeypatch,
        stream_chunks=[types.SimpleNamespace(candidates=[cand], usage_metadata=None)],
    )

    provider = GeminiProvider()
    events = [
        ev
        async for ev in provider.stream(
            [], [], model="gemini-2.5-flash", vertex_google_search=True
        )
    ]

    assert {"google_search": {}} in captured_config["config"]["tools"]
    assert not any(isinstance(ev, GroundingEvent) for ev in events)
    # Vertex AI (Gemini Enterprise Agent Platform) rejects `ToolConfig(include_
    # server_side_tool_invocations=...)`: it's Gemini Developer API (AI Studio) only.
    assert "tool_config" not in captured_config["config"]


@pytest.mark.asyncio
async def test_stream_combines_google_search_with_function_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real agent turns always carry function-call tools (read_file, run_command, ...);

    grounding must be wired in *alongside* them, not only when ``tools=[]``. This is a
    mocked structural check that both ``Tool`` entries are sent together — it does not
    prove the real Vertex API accepts the combination for a given model generation (see
    the live test in tests/integration/test_vertex_google_search_live.py for that).
    """
    cand = types.SimpleNamespace(
        content=types.SimpleNamespace(parts=[types.SimpleNamespace(text="hi", function_call=None)]),
        grounding_metadata=None,
    )
    captured_config = _install_fake_google_genai(
        monkeypatch,
        stream_chunks=[types.SimpleNamespace(candidates=[cand], usage_metadata=None)],
    )

    provider = GeminiProvider()
    tool = ToolDef("read_file", "Read a file", {"type": "object", "properties": {}})
    [
        ev
        async for ev in provider.stream(
            [], [tool], model="gemini-2.5-flash", vertex_google_search=True
        )
    ]

    tools = captured_config["config"]["tools"]
    assert {"google_search": {}} in tools
    assert any("function_declarations" in t for t in tools)


@pytest.mark.asyncio
async def test_stream_omits_google_search_tool_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_config = _install_fake_google_genai(monkeypatch, stream_chunks=[])

    provider = GeminiProvider()
    [ev async for ev in provider.stream([], [], model="gemini-2.5-flash")]

    assert "tools" not in captured_config["config"]


def test_estimate_developer_api_tool_tokens_includes_schemas() -> None:
    tool = ToolDef("read_file", "Read a file", {"type": "object", "properties": {"path": {}}})
    assert _estimate_developer_api_tool_tokens([]) == 0
    n = _estimate_developer_api_tool_tokens([tool])
    assert n > 0
    with_search = _estimate_developer_api_tool_tokens([tool], vertex_google_search=True)
    assert with_search > n


@pytest.mark.asyncio
async def test_count_tokens_developer_api_folds_system_omits_unsupported_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI Studio (api_key) CountTokensConfig rejects system_instruction/tools/gen_cfg."""
    captured = _install_fake_google_genai(monkeypatch)
    provider = GeminiProvider(api_key="test-key")
    messages = [
        Message.text("system", "You are helpful."),
        Message.text("user", "hi"),
    ]
    tool = ToolDef("read_file", "Read a file", {"type": "object", "properties": {}})

    n = await provider.count_input_tokens(messages, [tool], model="gemini-2.5-flash")

    tool_est = _estimate_developer_api_tool_tokens([tool])
    assert tool_est > 0
    assert n == 42 + tool_est
    assert captured["client_kwargs"].get("api_key") == "test-key"
    assert captured["config"] is None
    contents = captured["contents"]
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "You are helpful."


@pytest.mark.asyncio
async def test_count_tokens_developer_api_adds_google_search_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even on api_key, stream may send google_search; count should not drop it."""
    _install_fake_google_genai(monkeypatch)
    provider = GeminiProvider(api_key="test-key")

    n = await provider.count_input_tokens(
        [], [], model="gemini-2.5-flash", vertex_google_search=True
    )

    assert n == 42 + _estimate_developer_api_tool_tokens([], vertex_google_search=True)


@pytest.mark.asyncio
async def test_count_tokens_vertex_keeps_system_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_google_genai(monkeypatch)
    provider = GeminiProvider()
    messages = [
        Message.text("system", "You are helpful."),
        Message.text("user", "hi"),
    ]

    n = await provider.count_input_tokens(messages, [], model="gemini-2.5-flash")

    assert n == 42
    assert captured["client_kwargs"].get("vertexai") is True
    assert captured["config"]["system_instruction"] == "You are helpful."
    assert "generation_config" in captured["config"]


@pytest.mark.asyncio
async def test_count_tokens_error_logs_before_wrapping(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_fake_google_genai(monkeypatch, count_tokens_error=RuntimeError("boom"))

    provider = GeminiProvider()
    with (
        caplog.at_level("WARNING", logger="monkeybot.providers.gemini"),
        pytest.raises(LLMError, match="boom"),
    ):
        await provider.count_input_tokens([], [], model="gemini-2.5-flash")

    assert "Gemini count_tokens error" in caplog.text
    assert "provider=gemini" in caplog.text
    assert "model=gemini-2.5-flash" in caplog.text


@pytest.mark.asyncio
async def test_stream_error_logs_before_wrapping(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeModels:
        async def generate_content_stream(self, **_kwargs: Any) -> Any:
            raise RuntimeError("boom")

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.aio = types.SimpleNamespace(models=FakeModels())

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_types = ModuleType("google.genai.types")
    fake_genai.Client = FakeClient  # type: ignore[attr-defined]
    fake_types.GenerateContentConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.ThinkingConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.Tool = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.FunctionDeclaration = lambda **kw: kw  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    fake_genai.types = fake_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "p")

    provider = GeminiProvider()
    with (
        caplog.at_level("WARNING", logger="monkeybot.providers.gemini"),
        pytest.raises(LLMError, match="boom"),
    ):
        [ev async for ev in provider.stream([], [], model="gemini-2.5-flash")]

    assert "Gemini stream error" in caplog.text
    assert "provider=gemini" in caplog.text
    assert "model=gemini-2.5-flash" in caplog.text
