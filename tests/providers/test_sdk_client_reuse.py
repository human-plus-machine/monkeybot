"""One SDK HTTP client per endpoint, with the model and affinity header per request."""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import ModuleType, SimpleNamespace
from typing import Any

import aiohttp
import pytest

from monkeybot.core.llm.provider import Message, ProviderCallHints
from monkeybot.providers._openai_compat import stream_chat_completions_with_tool_fallback
from monkeybot.providers._sdk_client import (
    _clients,
    aclose_sdk_clients,
    cached_sdk_client,
    clear_sdk_clients,
)
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


@pytest.mark.asyncio
async def test_gemini_client_follows_endpoint_not_model(monkeypatch: pytest.MonkeyPatch) -> None:
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


class _LocalOkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_cached_client_is_not_reused_after_its_event_loop_closes() -> None:
    """A client bound to a finished ``asyncio.run`` must not be handed out again.

    google-genai uses ``aiohttp.ClientSession`` for async calls when aiohttp is
    installed. Reusing that session after ``asyncio.run`` has closed the loop
    raises ``RuntimeError('Event loop is closed')`` (aiohttp 3.14.1).
    """
    server = HTTPServer(("127.0.0.1", 0), _LocalOkHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    sessions: list[aiohttp.ClientSession] = []

    def factory() -> aiohttp.ClientSession:
        session = aiohttp.ClientSession()
        sessions.append(session)
        return session

    async def use_twice() -> int:
        first = cached_sdk_client(("aiohttp-loop",), factory)
        second = cached_sdk_client(("aiohttp-loop",), factory)
        assert first is second
        async with first.get(url) as response:
            await response.text()
            return response.status

    async def close_sessions() -> None:
        for session in sessions:
            if not session.closed:
                await session.close()

    try:
        assert asyncio.run(use_twice()) == 200
        assert len(sessions) == 1
        assert asyncio.run(use_twice()) == 200
        assert len(sessions) == 2
    finally:
        asyncio.run(close_sessions())
        server.shutdown()


def test_clear_sdk_clients_closes_openai_and_genai_clients() -> None:
    """``clear_sdk_clients`` must close cached HTTP pools.

    OpenAI and Anthropic expose ``async def close`` (``AsyncAPIClient.close``
    awaits the httpx client's ``aclose``). google-genai's async pool is
    ``Client.aclose``; its sync ``Client.close`` does not close that pool.
    """

    class _OpenAIShape:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _GenaiShape:
        def __init__(self) -> None:
            self.async_closed = False
            self.sync_closed = False

        async def aclose(self) -> None:
            self.async_closed = True

        def close(self) -> None:
            self.sync_closed = True

    holder: dict[str, Any] = {}

    async def build() -> None:
        holder["openai"] = cached_sdk_client(("shape-openai",), _OpenAIShape)
        holder["genai"] = cached_sdk_client(("shape-genai",), _GenaiShape)

    asyncio.run(build())
    clear_sdk_clients()
    assert holder["openai"].closed is True
    assert holder["genai"].async_closed is True
    assert holder["genai"].sync_closed is False


async def test_cache_keys_do_not_store_raw_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-live-review-secret"
    seen = _install_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    provider = OpenAIProvider()
    await _drain_openai(provider, model="gpt-4.1")
    await _drain_openai(provider, model="gpt-4.1")
    assert len(seen["constructed"]) == 1

    before_compat = len(seen["constructed"])
    await _drain_compat(secret)
    await _drain_compat(secret)
    assert len(seen["constructed"]) == before_compat + 1

    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    _install_claude(monkeypatch)
    claude = ClaudeProvider()
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-opus-4-1"):
        pass

    _install_genai(monkeypatch)
    gemini = GeminiProvider(api_key=secret)
    assert gemini._client("gemini-2.5-flash") is gemini._client("gemini-2.5-pro")

    assert _clients
    assert secret not in repr(_clients)


def _compat_stream(api_key: str) -> Any:
    return stream_chat_completions_with_tool_fallback(
        base_url="https://example.test/v1",
        api_key=api_key,
        provider="openrouter",
        messages=[Message.text("user", "hi")],
        tools=[],
        model="model-a",
        temperature=0.0,
        max_tokens=16,
    )


async def _drain_compat(api_key: str) -> None:
    async for _event in _compat_stream(api_key):
        pass


def _install_claude(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    constructed: list[Any] = []
    client = make_anthropic_stream_mock(_claude_events())

    def _factory(**_kwargs: Any) -> Any:
        constructed.append(client)
        return client

    monkeypatch.setattr("anthropic.AsyncAnthropic", _factory)
    return constructed


def _install_genai(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            return

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = _Client  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)


async def test_openai_and_claude_cache_keys_follow_sdk_endpoint_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OpenAI and Anthropic SDKs read these at construction time.

    Confirmed in the installed SDKs: ``OPENAI_BASE_URL``, ``OPENAI_ORG_ID``,
    and ``OPENAI_PROJECT_ID`` (openai/_client.py) and ``ANTHROPIC_BASE_URL``
    plus ``ANTHROPIC_AUTH_TOKEN`` (anthropic/_client.py).
    """
    seen = _install_openai(monkeypatch)
    monkeypatch.delenv("OPENAI_ORG_ID", raising=False)
    monkeypatch.delenv("OPENAI_PROJECT_ID", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://a.example/v1")
    provider = OpenAIProvider()
    await _drain_openai(provider, model="gpt-4.1")
    await _drain_openai(provider, model="gpt-4.1")
    assert len(seen["constructed"]) == 1

    monkeypatch.setenv("OPENAI_BASE_URL", "https://b.example/v1")
    await _drain_openai(provider, model="gpt-4.1")
    assert len(seen["constructed"]) == 2

    monkeypatch.setenv("OPENAI_ORG_ID", "org-123")
    await _drain_openai(provider, model="gpt-4.1")
    assert len(seen["constructed"]) == 3

    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-123")
    await _drain_openai(provider, model="gpt-4.1")
    assert len(seen["constructed"]) == 4

    await _drain_openai(provider, model="gpt-5")
    assert len(seen["constructed"]) == 4

    constructed = _install_claude(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    claude = ClaudeProvider()
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    assert len(constructed) == 1

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude.example/v1")
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    assert len(constructed) == 2

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ant-token")
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    assert len(constructed) == 3


# Review follow-up on PR 300. Error text is CPython's own: ``asyncio`` raises
# ``RuntimeError('Event loop is closed')`` from ``BaseEventLoop._check_closed``.


def _cached_slot_count() -> int:
    return sum(len(slots) for slots in _clients.values())


@pytest.mark.asyncio
async def test_one_failing_close_does_not_skip_the_other_clients() -> None:
    class _ClosedLoopClient:
        async def close(self) -> None:
            raise RuntimeError("Event loop is closed")

    class _OpenAIShape:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    cached_sdk_client(("close-fails",), _ClosedLoopClient)
    healthy = cached_sdk_client(("close-ok",), _OpenAIShape)

    await aclose_sdk_clients()

    assert healthy.closed is True
    assert not _clients


@pytest.mark.asyncio
async def test_rotated_credentials_replace_the_cached_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = _install_claude(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-old")
    claude = ClaudeProvider()
    for api_key in ("sk-ant-old", "sk-ant-new"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
        async for _event in claude.stream(
            [Message.text("user", "hi")], [], model="claude-sonnet-4-5"
        ):
            pass
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude.example/v1")
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass

    assert len(constructed) == 3
    assert _cached_slot_count() == 1


@pytest.mark.asyncio
async def test_bedrock_client_follows_the_bearer_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AsyncAnthropicBedrock`` reads ``AWS_BEARER_TOKEN_BEDROCK`` and
    ``ANTHROPIC_BEDROCK_BASE_URL`` in its constructor (anthropic 0.102.0)."""
    from monkeybot.providers.bedrock import BedrockProvider

    constructed: list[object] = []

    def _factory(**_kwargs: Any) -> object:
        client = object()
        constructed.append(client)
        return client

    monkeypatch.setattr("anthropic.AsyncAnthropicBedrock", _factory)
    provider = BedrockProvider(aws_region="us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-token-1")
    first = provider._client()
    assert provider._client() is first
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-token-2")
    assert provider._client() is not first
    assert len(constructed) == 2


@pytest.mark.asyncio
async def test_sync_callers_get_an_uncached_client() -> None:
    """A client built with no running loop is not handed to later ``asyncio.run`` calls."""

    def build() -> object:
        return cached_sdk_client(("sync-built",), object)

    first = await asyncio.to_thread(build)
    second = await asyncio.to_thread(build)

    assert first is not second
    assert ("sync-built",) not in _clients


@pytest.mark.asyncio
async def test_openai_compat_client_follows_openai_org_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AsyncOpenAI(base_url=..., api_key=...)`` still reads ``OPENAI_ORG_ID`` and
    ``OPENAI_PROJECT_ID`` (openai 2.36.0)."""
    seen = _install_openai(monkeypatch)
    monkeypatch.delenv("OPENAI_ORG_ID", raising=False)
    await _drain_compat("compat-key")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-123")
    await _drain_compat("compat-key")

    assert len(seen["constructed"]) == 2


def test_building_a_client_does_not_block_other_threads() -> None:
    """A slow constructor (``google.auth.default`` on GCE) must not hold the cache lock."""
    started = threading.Event()
    release = threading.Event()
    other_done = threading.Event()

    def slow_factory() -> object:
        started.set()
        release.wait(5)
        return object()

    async def build(key: tuple[str, ...], factory: Any) -> None:
        cached_sdk_client(key, factory)

    slow = threading.Thread(target=lambda: asyncio.run(build(("slow",), slow_factory)))
    slow.start()
    try:
        assert started.wait(5)
        fast = threading.Thread(
            target=lambda: (asyncio.run(build(("fast",), object)), other_done.set())
        )
        fast.start()
        assert other_done.wait(1), "fast lookup waited on the slow constructor"
    finally:
        release.set()
        slow.join(5)


@pytest.mark.asyncio
async def test_clear_sdk_clients_refuses_to_orphan_clients_on_a_running_loop() -> None:
    class _OpenAIShape:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = cached_sdk_client(("running-loop",), _OpenAIShape)

    with pytest.raises(RuntimeError, match="aclose_sdk_clients"):
        clear_sdk_clients()

    assert cached_sdk_client(("running-loop",), _OpenAIShape) is client
    await aclose_sdk_clients()
    assert client.closed is True


@pytest.mark.asyncio
async def test_any_sdk_env_var_change_rebuilds_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both SDKs read ``*_CUSTOM_HEADERS`` in their constructors (openai 2.36.0,
    anthropic 0.102.0); no call site listed it."""
    seen = _install_openai(monkeypatch)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    provider = OpenAIProvider()
    await _drain_openai(provider, model="gpt-4.1")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "x-team: search")
    await _drain_openai(provider, model="gpt-4.1")
    assert len(seen["constructed"]) == 2

    constructed = _install_claude(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    claude = ClaudeProvider()
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-team: search")
    async for _event in claude.stream([Message.text("user", "hi")], [], model="claude-sonnet-4-5"):
        pass
    assert len(constructed) == 2
