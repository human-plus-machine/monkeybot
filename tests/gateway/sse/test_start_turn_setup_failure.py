"""A setup failure before run_loop must not lose the user's prompt."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.gateway.sse.app import GatewayLoopPort
from monkeybot.gateway.sse.session_bus import SessionBus, SessionRegistry


class _NamedProvider:
    name = "fake"


def _wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    async def _failing_build_context(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("memory backend unavailable")

    run_loop = MagicMock()
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# agent\n", encoding="utf-8")

    monkeypatch.setattr(gateway_app, "transcript_enabled_from_config", lambda: False)
    monkeypatch.setattr(gateway_app, "build_context", _failing_build_context)
    monkeypatch.setattr(gateway_app, "run_loop", run_loop)
    monkeypatch.setattr(gateway_app, "_default_agent_path", lambda _bus: agent_md)
    monkeypatch.setattr(
        gateway_app,
        "_resolved_workspace_paths",
        lambda *_a, **_k: (tmp_path, tmp_path / "skills", tmp_path / "artifacts"),
    )
    gateway_app.gateway_runtime.mcp = MagicMock()
    gateway_app.gateway_runtime.mcp.catalog_names.return_value = []
    gateway_app.gateway_runtime.provider = _NamedProvider()
    gateway_app.gateway_runtime.inspectors = []
    gateway_app.gateway_runtime.hook_manager = None
    gateway_app.gateway_runtime.web_search_tool = None

    history = MagicMock()
    history.load = AsyncMock(return_value=[])
    history.append = AsyncMock()
    storage = MagicMock()
    storage.history.return_value = history
    storage.branches.return_value.get_active = AsyncMock(return_value=None)
    storage.usage.return_value = AsyncMock()
    gateway_app.app.state.storage = storage
    return history


@pytest.mark.asyncio
async def test_build_context_failure_keeps_user_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    published: list[str] = []

    async def _capture(self: SessionBus, data_json: str, *, lane: str = "primary") -> int:
        del self, lane
        published.append(data_json)
        return 1

    monkeypatch.setattr(SessionBus, "publish_data", _capture)
    history = _wire(monkeypatch, tmp_path)
    registry = SessionRegistry()
    registry.create("s1", agent_md=None, created_at_ms=0)

    await GatewayLoopPort(registry).start_turn("s1", "req-1", [Text(text="my long prompt")])

    assert history.append.await_count == 1
    appended = history.append.await_args
    message = appended.args[1]
    assert message.role == "user"
    assert "my long prompt" in str(message.content)
    assert [json.loads(p)["type"] for p in published] == ["Error", "TurnComplete"]
