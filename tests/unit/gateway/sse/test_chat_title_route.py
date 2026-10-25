"""POST /api/chat-title names a chat after the first reply, and stays quiet on failure."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry

SESSION = "s1"


class _RecordingProvider(ScriptedFakeProvider):
    def __init__(self, events: list[ProviderEvent]) -> None:
        super().__init__(events)
        self.calls = 0
        self.prompts: list[str] = []

    async def stream(
        self,
        messages: Any,
        tools: Any,
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        block = messages[-1].content[0]
        assert isinstance(block, Text)
        self.prompts.append(block.text)
        async for event in super().stream(
            messages, tools, model=model, thinking_budget=thinking_budget
        ):
            yield event


class _FailingProvider:
    @property
    def name(self) -> str:
        return "fail"

    @property
    def supports_streaming(self) -> bool:
        return False

    async def stream(
        self,
        messages: Any,
        tools: Any,
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, model, thinking_budget
        raise RuntimeError("provider down")
        yield TextDelta(text="")

    async def count_input_tokens(self, messages: Any, tools: Any, *, model: str) -> int:
        del messages, tools, model
        return 0


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[SQLiteStorageBackend]:
    storage = SQLiteStorageBackend("sqlite:///:memory:")
    await storage.open()
    yield storage
    await storage.close()


@pytest_asyncio.fixture
async def client(backend: SQLiteStorageBackend) -> AsyncIterator[dict[str, Any]]:
    registry = SessionRegistry()
    registry.create(SESSION, agent_md=None, created_at_ms=0)
    app = create_app(registry=registry)
    app.state.storage = backend
    provider = _RecordingProvider([TextDelta(text='"Fixing the login bug."'), Done()])
    app.state.gateway_runtime = SimpleNamespace(provider=provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"client": http, "app": app, "backend": backend, "provider": provider}


async def _seed(backend: SQLiteStorageBackend, *texts: str) -> None:
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        await backend.history().append(SESSION, Message(role=role, content=[Text(text=text)]))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_title_route_names_a_chat_after_the_reply(client: dict[str, Any]) -> None:
    await _seed(client["backend"], "why does login fail", "the token is expired")
    response = await client["client"].post("/api/chat-title", json={"session_id": SESSION})
    assert response.status_code == 200, response.text
    assert response.json() == {"title": "Fixing the login bug"}
    assert client["provider"].calls == 1


@pytest.mark.asyncio
async def test_title_route_returns_null_before_the_assistant_replies(
    client: dict[str, Any],
) -> None:
    await _seed(client["backend"], "why does login fail")
    response = await client["client"].post("/api/chat-title", json={"session_id": SESSION})
    assert response.status_code == 200, response.text
    assert response.json() == {"title": None}
    assert client["provider"].calls == 0


@pytest.mark.asyncio
async def test_title_route_returns_null_when_the_provider_fails(client: dict[str, Any]) -> None:
    await _seed(client["backend"], "why does login fail", "the token is expired")
    client["app"].state.gateway_runtime = SimpleNamespace(provider=_FailingProvider())
    response = await client["client"].post("/api/chat-title", json={"session_id": SESSION})
    assert response.status_code == 200, response.text
    assert response.json() == {"title": None}


@pytest.mark.asyncio
async def test_title_route_names_from_the_opening_turn(client: dict[str, Any]) -> None:
    texts: list[str] = []
    for index in range(25):
        texts.extend((f"user-{index}", f"assistant-{index}"))
    await _seed(client["backend"], *texts)
    response = await client["client"].post("/api/chat-title", json={"session_id": SESSION})
    assert response.status_code == 200, response.text
    [prompt] = client["provider"].prompts
    assert "user-0" in prompt
    assert "assistant-0" in prompt
    assert "user-24" not in prompt
    assert "assistant-24" not in prompt


@pytest.mark.asyncio
async def test_title_route_returns_null_without_a_provider(client: dict[str, Any]) -> None:
    await _seed(client["backend"], "why does login fail", "the token is expired")
    client["app"].state.gateway_runtime = SimpleNamespace(provider=None)
    response = await client["client"].post("/api/chat-title", json={"session_id": SESSION})
    assert response.status_code == 200, response.text
    assert response.json() == {"title": None}
    assert client["provider"].calls == 0
