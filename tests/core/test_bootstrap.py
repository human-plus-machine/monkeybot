"""Tests for :mod:`monkeybot.core.bootstrap`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monkeybot.core.bootstrap import (
    PatternBcTurnError,
    create_harness_deps,
    run_pattern_bc_turn,
)
from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.events import Error, TurnComplete, UsageTotals
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.terminal import ALLOWED_COMMANDS


def _fake() -> ScriptedFakeProvider:
    return ScriptedFakeProvider(
        [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
    )


@pytest.mark.asyncio
async def test_create_harness_deps_opens_storage() -> None:
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        _provider_override=_fake(),
    )
    assert await deps.storage.history().load("t", 1) == []
    await deps.close()


@pytest.mark.asyncio
async def test_create_harness_deps_no_memory_when_uri_none() -> None:
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        _provider_override=_fake(),
    )
    assert deps.memory is None
    await deps.close()


@pytest.mark.asyncio
async def test_create_harness_deps_memory_set_when_uri_given(tmp_path: Path) -> None:
    mem_root = tmp_path / "mem"
    mem_root.mkdir()
    uri = "local://" + str(mem_root.resolve())
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        uri,
        open_mcp=False,
        _provider_override=_fake(),
    )
    assert isinstance(deps.memory, MemorySubsystem)
    assert deps.memory.uri == uri
    await deps.close()


@pytest.mark.asyncio
async def test_create_harness_deps_mcp_empty_when_open_mcp_false() -> None:
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        _provider_override=_fake(),
    )
    assert deps.mcp.all_tools() == []
    await deps.close()


@pytest.mark.asyncio
async def test_create_harness_deps_open_mcp_true_skips_load_when_path_none() -> None:
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=True,
        mcp_config_path=None,
        _provider_override=_fake(),
    )
    assert deps.mcp.all_tools() == []
    await deps.close()


@pytest.mark.asyncio
async def test_create_harness_deps_open_mcp_true_empty_mcp_servers(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {}}', encoding="utf-8")
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=True,
        mcp_config_path=cfg,
        _provider_override=_fake(),
    )
    assert deps.mcp.all_tools() == []
    await deps.close()


@pytest.mark.asyncio
async def test_create_harness_deps_closes_backend_on_memory_subsystem_failure(
    tmp_path: Path,
) -> None:
    backend = MagicMock()
    backend.open = AsyncMock()
    backend.close = AsyncMock()
    mem_root = tmp_path / "mem"
    mem_root.mkdir()
    uri = "local://" + str(mem_root.resolve())
    with patch("monkeybot.core.bootstrap.create_storage_backend", return_value=backend), patch(
        "monkeybot.core.bootstrap.MemorySubsystem",
        side_effect=RuntimeError("boom"),
    ), pytest.raises(RuntimeError, match="boom"):
        await create_harness_deps(
            "sqlite:///:memory:",
            uri,
            open_mcp=False,
            _provider_override=_fake(),
        )
    backend.open.assert_awaited_once()
    backend.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_closes_storage() -> None:
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        _provider_override=_fake(),
    )
    await deps.close()
    with pytest.raises(RuntimeError, match="open\\(\\) has not been called"):
        deps.storage.history()


@pytest.mark.asyncio
async def test_close_await_disconnect_all() -> None:
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        _provider_override=_fake(),
    )
    mock_disconnect = AsyncMock()
    deps.mcp.disconnect_all = mock_disconnect
    await deps.close()
    mock_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_override_bypasses_get_provider_config() -> None:
    fake = _fake()
    with patch(
        "monkeybot.core.bootstrap.get_provider_config",
        side_effect=AssertionError("get_provider_config must not run when override is set"),
    ):
        deps = await create_harness_deps(
            "sqlite:///:memory:",
            None,
            open_mcp=False,
            _provider_override=fake,
        )
    assert deps.provider is fake
    await deps.close()


@pytest.mark.asyncio
async def test_run_pattern_bc_turn_smoke(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Agent\n\nNon-empty.", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        _provider_override=_fake(),
    )
    try:
        text = await run_pattern_bc_turn(
            deps,
            "hello",
            session_id="sid",
            request_id="rid",
            agent_md_path=agent,
            skills_path=tmp_path / "skills",
            workspace_root=ws,
            hook_manager=None,
        )
        assert text == "x"
    finally:
        await deps.close()


@pytest.mark.asyncio
async def test_run_pattern_bc_turn_raises_on_error_event(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Agent\n\nNon-empty.", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()

    async def _error_loop(*_args, **_kwargs):
        yield Error(request_id="rid", error="provider blew up")
        yield TurnComplete(request_id="rid", usage=UsageTotals())

    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        provider_override=_fake(),
    )
    try:
        with (
            patch("monkeybot.core.bootstrap.run_loop", _error_loop),
            pytest.raises(PatternBcTurnError, match="provider blew up") as excinfo,
        ):
            await run_pattern_bc_turn(
                deps,
                "hello",
                session_id="sid",
                request_id="rid",
                agent_md_path=agent,
                skills_path=tmp_path / "skills",
                workspace_root=ws,
            )
        assert excinfo.value.request_id == "rid"
    finally:
        await deps.close()


@pytest.mark.asyncio
async def test_run_pattern_bc_turn_default_run_command_allowlist(tmp_path: Path, monkeypatch) -> None:
    """Bootstrap must not pass an empty allowlist (issue #7)."""
    monkeypatch.delenv("COMMAND_ALLOWLIST_CONFIG", raising=False)
    (tmp_path / "skills").mkdir()
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Agent\n\nNon-empty.", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    deps = await create_harness_deps(
        "sqlite:///:memory:",
        None,
        open_mcp=False,
        provider_override=_fake(),
    )
    captured: list = []

    async def _capture_run_loop(*args, **kwargs):
        captured.append(kwargs.get("tool_executor"))
        if False:
            yield  # pragma: no cover — makes this an async generator

    with patch("monkeybot.core.bootstrap.run_loop", _capture_run_loop):
        try:
            await run_pattern_bc_turn(
                deps,
                "hi",
                session_id="sid",
                request_id="rid",
                agent_md_path=agent,
                skills_path=tmp_path / "skills",
                workspace_root=ws,
            )
        finally:
            await deps.close()

    assert len(captured) == 1
    executor = captured[0]
    assert tuple(executor._run_cmd_allowed_commands) == tuple(ALLOWED_COMMANDS)
