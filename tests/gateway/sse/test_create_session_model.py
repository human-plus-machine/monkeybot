"""Tests for per-session model selection on POST /sessions and GatewayLoopPort."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.config.settings import ProviderConfig
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.types.content_blocks import ContentBlock, Text
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.gateway.sse.app import GatewayLoopPort
from monkeybot.gateway.sse.loop_port import LoopPort
from monkeybot.gateway.sse.models import CreateSessionRequest
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry


from monkeybot.core.types.content_blocks import ContentBlock, Text


class _NoopLoopPort:
    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> None:
        _ = (session_id, request_id, user_content)


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


@pytest.fixture
def app(registry: SessionRegistry):
    loop: LoopPort = _NoopLoopPort()
    return create_app(loop_port=loop, registry=registry)


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# --- Task 1: CreateSessionRequest model fields ---


def test_create_session_request_model_fields_optional() -> None:
    req = CreateSessionRequest()
    assert req.model_provider is None and req.model_name is None


def test_create_session_request_accepts_model() -> None:
    req = CreateSessionRequest(model_provider="openai", model_name="gpt-5")
    assert req.model_provider == "openai"
    assert req.model_name == "gpt-5"


# --- Task 2: SessionRegistry stores provider/model ---


def test_registry_create_stores_provider_model() -> None:
    reg = SessionRegistry()
    obj = object()
    bus = reg.create("s", agent_md=None, created_at_ms=0, provider=obj, model_name="gpt-5")
    assert bus.provider is obj
    assert bus.model_name == "gpt-5"


def test_registry_create_defaults_none() -> None:
    reg = SessionRegistry()
    bus = reg.create("s", agent_md=None, created_at_ms=0)
    assert bus.provider is None
    assert bus.model_name is None


# --- Task 3: create_session route integration ---


@pytest.mark.asyncio
async def test_create_session_no_model_uses_default(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    r = await client.post("/sessions", json={})
    assert r.status_code == 201
    sid = r.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    assert bus.provider is None
    assert bus.model_name is None


@pytest.mark.asyncio
async def test_create_session_with_model_stores_provider(
    client: AsyncClient,
    registry: SessionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_provider = object()

    def _fake_get_provider_config(**kwargs: object) -> ProviderConfig:
        assert kwargs.get("provider") == "openai"
        assert kwargs.get("model_name") == "gpt-5"
        return ProviderConfig(provider=fake_provider, model="gpt-5")  # type: ignore[arg-type]

    monkeypatch.setattr(
        "monkeybot.core.config.settings.get_provider_config",
        _fake_get_provider_config,
    )

    r = await client.post(
        "/sessions",
        json={"model_provider": "openai", "model_name": "gpt-5"},
    )
    assert r.status_code == 201
    sid = r.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    assert bus.provider is fake_provider
    assert bus.model_name == "gpt-5"


@pytest.mark.asyncio
async def test_create_session_model_unavailable_returns_400(
    client: AsyncClient,
    registry: SessionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_unavailable(**_kwargs: object) -> ProviderConfig:
        raise ValueError("missing OPENAI_API_KEY")

    monkeypatch.setattr(
        "monkeybot.core.config.settings.get_provider_config",
        _raise_unavailable,
    )

    r = await client.post(
        "/sessions",
        json={
            "session_id": "unavail-session",
            "model_provider": "openai",
            "model_name": "gpt-5",
        },
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "MODEL_UNAVAILABLE"
    assert "openai" in err["message"].lower()
    assert registry.get("unavail-session") is None


# --- Task 4: GatewayLoopPort.start_turn uses session provider/model ---


class _FakeExecutor:
    async def aclose(self) -> None:
        return


@pytest.mark.asyncio
async def test_start_turn_prefers_session_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = SessionRegistry()
    fake_session_provider = object()
    global_provider = object()
    registry.create(
        "s1",
        agent_md=None,
        created_at_ms=0,
        provider=fake_session_provider,
        model_name="gpt-5",
    )

    captured_build: dict[str, object] = {}
    captured_run: dict[str, object] = {}

    async def _fake_build_context(*_args: object, **kwargs: object) -> MagicMock:
        captured_build.update(kwargs)
        return MagicMock()

    async def _fake_run_loop(*_args: object, **kwargs: object):
        captured_run.update(kwargs)
        yield TurnComplete(request_id="r1", usage=UsageTotals())

    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# agent\n", encoding="utf-8")

    monkeypatch.setattr(gateway_app, "build_context", _fake_build_context)
    monkeypatch.setattr(gateway_app, "run_loop", _fake_run_loop)
    monkeypatch.setattr(gateway_app, "CoreToolExecutor", lambda **_kw: _FakeExecutor())
    monkeypatch.setattr(gateway_app, "_default_agent_path", lambda _bus: agent_md)
    monkeypatch.setattr(
        gateway_app,
        "_resolved_workspace_paths",
        lambda: (tmp_path, tmp_path / "skills"),
    )

    gateway_app._deps.mcp = MagicMock()
    gateway_app._deps.provider = global_provider
    gateway_app._deps.inspectors = []
    gateway_app._deps.curator_provider = global_provider
    gateway_app._deps.hook_manager = None
    gateway_app._deps.web_search_tool = None

    mock_usage = AsyncMock()
    mock_history = MagicMock()
    mock_history.load = AsyncMock(return_value=[])
    mock_storage = MagicMock()
    mock_storage.history.return_value = mock_history
    mock_storage.usage.return_value = mock_usage
    gateway_app.app.state.storage = mock_storage

    port = GatewayLoopPort(registry)
    await port.start_turn("s1", "req-1", [Text(text="hello")])

    assert captured_run.get("provider") is fake_session_provider
    assert captured_build.get("model") == "gpt-5"


@pytest.mark.asyncio
async def test_start_turn_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = SessionRegistry()
    registry.create("s2", agent_md=None, created_at_ms=0)

    global_provider = object()
    captured_build: dict[str, object] = {}
    captured_run: dict[str, object] = {}

    async def _fake_build_context(*_args: object, **kwargs: object) -> MagicMock:
        captured_build.update(kwargs)
        return MagicMock()

    async def _fake_run_loop(*_args: object, **kwargs: object):
        captured_run.update(kwargs)
        yield TurnComplete(request_id="r2", usage=UsageTotals())

    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# agent\n", encoding="utf-8")

    monkeypatch.setenv("MODEL_NAME", "env-default-model")
    monkeypatch.setattr(gateway_app, "build_context", _fake_build_context)
    monkeypatch.setattr(gateway_app, "run_loop", _fake_run_loop)
    monkeypatch.setattr(gateway_app, "CoreToolExecutor", lambda **_kw: _FakeExecutor())
    monkeypatch.setattr(gateway_app, "_default_agent_path", lambda _bus: agent_md)
    monkeypatch.setattr(
        gateway_app,
        "_resolved_workspace_paths",
        lambda: (tmp_path, tmp_path / "skills"),
    )

    gateway_app._deps.mcp = MagicMock()
    gateway_app._deps.provider = global_provider
    gateway_app._deps.inspectors = []
    gateway_app._deps.curator_provider = global_provider
    gateway_app._deps.hook_manager = None
    gateway_app._deps.web_search_tool = None

    mock_usage = AsyncMock()
    mock_history = MagicMock()
    mock_history.load = AsyncMock(return_value=[])
    mock_storage = MagicMock()
    mock_storage.history.return_value = mock_history
    mock_storage.usage.return_value = mock_usage
    gateway_app.app.state.storage = mock_storage

    port = GatewayLoopPort(registry)
    await port.start_turn("s2", "req-2", [Text(text="hello")])

    assert captured_run.get("provider") is global_provider
    assert captured_build.get("model") == "env-default-model"
