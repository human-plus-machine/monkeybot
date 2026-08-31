"""Tests for TranscriptWriter wiring in GatewayLoopPort.start_turn."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.runtime.events import AssistantDelta, TurnComplete, UsageTotals
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.gateway.sse.app import GatewayLoopPort
from monkeybot.gateway.sse.session_bus import SessionRegistry


class _FakeExecutor:
    async def aclose(self) -> None:
        return


class _NamedProvider:
    def __init__(self, name: str) -> None:
        self.name = name


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wire_start_turn_deps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider: object,
    captured_run: dict[str, object],
) -> None:
    async def _fake_build_context(*_args: object, **kwargs: object) -> MagicMock:
        return MagicMock()

    async def _fake_run_loop(*_args: object, **kwargs: object):
        captured_run.update(kwargs)
        yield AssistantDelta(request_id="req-1", delta="hi")
        yield TurnComplete(request_id="req-1", usage=UsageTotals())

    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# agent\n", encoding="utf-8")

    monkeypatch.setattr(gateway_app, "build_context", _fake_build_context)
    monkeypatch.setattr(gateway_app, "run_loop", _fake_run_loop)
    monkeypatch.setattr(gateway_app, "CoreToolExecutor", lambda **_kw: _FakeExecutor())
    monkeypatch.setattr(gateway_app, "_default_agent_path", lambda _bus: agent_md)
    monkeypatch.setattr(
        gateway_app,
        "_resolved_workspace_paths",
        lambda: (tmp_path, tmp_path / "skills", tmp_path / "artifacts"),
    )

    mcp = MagicMock()
    mcp.catalog_names.return_value = []
    gateway_app._deps.mcp = mcp
    gateway_app._deps.provider = provider
    gateway_app._deps.inspectors = []
    gateway_app._deps.hook_manager = None
    gateway_app._deps.web_search_tool = None

    mock_usage = AsyncMock()
    mock_history = MagicMock()
    mock_history.load = AsyncMock(return_value=[])
    mock_storage = MagicMock()
    mock_storage.history.return_value = mock_history
    mock_storage.usage.return_value = mock_usage
    gateway_app.app.state.storage = mock_storage


@pytest.mark.asyncio
async def test_start_turn_writes_transcript_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway_app, "transcript_enabled_from_config", lambda: True)
    registry = SessionRegistry()
    registry.create("s1", agent_md=None, created_at_ms=0)
    provider = _NamedProvider("fake")

    captured_run: dict[str, object] = {}
    _wire_start_turn_deps(monkeypatch, tmp_path, provider=provider, captured_run=captured_run)

    port = GatewayLoopPort(registry)
    await port.start_turn("s1", "req-1", [Text(text="hello")])

    assert captured_run.get("transcript_writer") is not None

    bus = registry.get("s1")
    assert bus is not None and bus.transcript_writer is not None
    transcript_path = bus.transcript_writer.path
    assert transcript_path.name == "transcript.ndjson"
    assert transcript_path.parent.name.endswith("_s1")
    assert transcript_path.is_file()
    lines = _read_lines(transcript_path)
    types = [line["type"] for line in lines]
    assert types[0] == "SessionManifest"
    assert "durable_only" not in lines[0]
    assert "harness_version" in lines[0]
    assert "UserMessage" in types
    assert "AssistantDelta" not in types  # live-only skipped by default
    assert "TurnComplete" in types


@pytest.mark.asyncio
async def test_start_turn_no_transcript_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway_app, "transcript_enabled_from_config", lambda: False)
    registry = SessionRegistry()
    registry.create("s2", agent_md=None, created_at_ms=0)
    provider = _NamedProvider("fake")

    captured_run: dict[str, object] = {}
    _wire_start_turn_deps(monkeypatch, tmp_path, provider=provider, captured_run=captured_run)

    port = GatewayLoopPort(registry)
    await port.start_turn("s2", "req-2", [Text(text="hello")])

    assert captured_run.get("transcript_writer") is None
    assert not (tmp_path / ".monkeybot" / "transcripts").exists()


@pytest.mark.asyncio
async def test_start_turn_reuses_transcript_writer_across_turns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway_app, "transcript_enabled_from_config", lambda: True)
    registry = SessionRegistry()
    bus = registry.create("s3", agent_md=None, created_at_ms=0)
    provider = _NamedProvider("fake")

    captured_run: dict[str, object] = {}
    _wire_start_turn_deps(monkeypatch, tmp_path, provider=provider, captured_run=captured_run)

    port = GatewayLoopPort(registry)
    await port.start_turn("s3", "req-1", [Text(text="hello")])
    writer_after_first = bus.transcript_writer
    await port.start_turn("s3", "req-2", [Text(text="again")])

    assert bus.transcript_writer is writer_after_first

    assert writer_after_first is not None
    transcript_path = writer_after_first.path
    assert transcript_path.name == "transcript.ndjson"
    lines = _read_lines(transcript_path)
    manifest_lines = [line for line in lines if line.get("type") == "SessionManifest"]
    assert len(manifest_lines) == 1
    user_messages = [line for line in lines if line.get("type") == "UserMessage"]
    assert len(user_messages) == 2
