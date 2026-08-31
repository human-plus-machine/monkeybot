"""Tests for GatewayRuntime.apply (transactional live-slice reload)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from monkeybot.core.config import (
    apply_monkeybot_runtime_env,
    get_config_store,
    reset_runtime_env_state_for_tests,
)
from monkeybot.core.config.runtime_env import ENV_MAP
from monkeybot.core.layout import AgentLayout
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.mcp.ports_mcp import MCPCatalogApplyResult
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.inspector import CommandTierInspector
from monkeybot.gateway.sse.app import (
    GatewayRuntime,
    RuntimeApplyResult,
    begin_in_flight_turn,
    end_in_flight_turn,
)
from monkeybot.gateway.sse.session_bus import SessionRegistry


@pytest.fixture(autouse=True)
def _reset_reload_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    env_before = {k: os.environ.get(k) for k in ENV_MAP.values()}
    yield
    from monkeybot.gateway.sse import app as sse_app

    sse_app._in_flight_turns = 0
    sse_app._turns_idle.set()
    reset_runtime_env_state_for_tests()
    for key, before_val in env_before.items():
        if before_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before_val


def _write_yaml(tmp_path: Path, body: str) -> Path:
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir(exist_ok=True)
    path = cfg_dir / "monkeybot.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _boot_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    monkeypatch.chdir(tmp_path)
    yaml_path = _write_yaml(tmp_path, body)
    for key in ("MODEL_NAME", "MODEL_PROVIDER", "MODEL_TEMPERATURE", "DB_URL"):
        monkeypatch.delenv(key, raising=False)
    apply_monkeybot_runtime_env()
    return yaml_path


def _app_with_runtime(runtime: GatewayRuntime) -> FastAPI:
    app = FastAPI()
    app.state.gateway_runtime = runtime
    return app


async def _apply_reload(
    runtime: GatewayRuntime, *, fastapi_app: FastAPI | None = None
) -> tuple[object, object, RuntimeApplyResult]:
    store = get_config_store()
    cfg, diff = store.prepare_reload()
    if diff.noop:
        return cfg, diff, RuntimeApplyResult()
    result = await runtime.apply(cfg, diff, fastapi_app=fastapi_app, registry=SessionRegistry())
    if result.error is None:
        cfg = store.commit(cfg)
    return cfg, diff, result


def _write_allowlist(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "allowed_commands:\n  - ls\nallowed_path_prefixes:\n  - ./\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_provider_rebuild_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n  temperature: 0.1\n",
    )
    runtime = GatewayRuntime()
    runtime.build_provider(get_config_store().current())
    assert isinstance(runtime.provider, ScriptedFakeProvider)
    old = runtime.provider

    yaml_path.write_text(
        "model:\n  provider: fake\n  temperature: 0.9\n",
        encoding="utf-8",
    )
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert "MODEL_TEMPERATURE" in result.applied
    assert result.error is None
    assert runtime.provider is not old
    assert isinstance(runtime.provider, ScriptedFakeProvider)


@pytest.mark.asyncio
async def test_applied_omits_unhandled_rebuild_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "MODEL_NAME",
        "MODEL_PROVIDER",
        "MODEL_TEMPERATURE",
        "DB_URL",
        "SANDBOX_ENABLED",
        "PENDING_RESPONSE_TIMEOUT_SEC",
        "MONKEYBOT_SCHEDULER_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\nsandbox:\n  enabled: false\n"
        "gateway:\n  pending_response_timeout_sec: 30\n"
        "scheduler:\n  enabled: false\n",
    )
    yaml_path.write_text(
        "model:\n  provider: fake\nsandbox:\n  enabled: true\n"
        "gateway:\n  pending_response_timeout_sec: 90\n"
        "scheduler:\n  enabled: true\n",
        encoding="utf-8",
    )
    _cfg, diff, result = await _apply_reload(GatewayRuntime())
    assert "SANDBOX_ENABLED" in diff.changed_env_keys
    assert "PENDING_RESPONSE_TIMEOUT_SEC" in diff.changed_env_keys
    assert "MONKEYBOT_SCHEDULER_ENABLED" in diff.changed_env_keys
    assert "SANDBOX_ENABLED" not in result.applied
    assert "PENDING_RESPONSE_TIMEOUT_SEC" not in result.applied
    assert "MONKEYBOT_SCHEDULER_ENABLED" not in result.applied


@pytest.mark.asyncio
async def test_missing_allowlist_reload_rejects_when_policy_was_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    _write_allowlist(allow)
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    runtime = GatewayRuntime()
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())
    old_inspectors = runtime.inspectors
    old_allowed = runtime.run_command_allowed_commands
    assert any(isinstance(i, CommandTierInspector) for i in old_inspectors)

    allow.unlink()
    yaml_path.write_text("model:\n  provider: fake\n  name: after\n", encoding="utf-8")
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert result.error is not None
    assert "command tier config missing" in result.error
    assert runtime.inspectors is old_inspectors
    assert runtime.run_command_allowed_commands is old_allowed
    assert get_config_store().current().revision == 1


def test_missing_allowlist_at_fresh_startup_allows_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    runtime = GatewayRuntime()
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())
    assert not any(isinstance(i, CommandTierInspector) for i in runtime.inspectors)
    assert runtime.run_command_allowed_commands is None


@pytest.mark.asyncio
async def test_failed_apply_leaves_live_slices_on_old_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n  temperature: 0.1\n"
        "subagents:\n  personas:\n    - name: helper\n      description: helps\n",
    )
    runtime = GatewayRuntime()
    runtime.build_provider(get_config_store().current())
    runtime.build_subagents()
    old_provider = runtime.provider
    yaml_path.write_text(
        "model:\n  provider: fake\n  temperature: 0.9\n"
        "subagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n"
        "    - name: helper\n      description: duplicate\n",
        encoding="utf-8",
    )
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert result.error is not None
    assert "Duplicate subagent" in result.error
    assert runtime.provider is old_provider
    assert get_config_store().current().revision == 1
    assert get_config_store().current().model.temperature == "0.1"


@pytest.mark.asyncio
async def test_yaml_env_change_reconnects_interpolated_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_json = tmp_path / "monkeybot_config" / "mcp.json"
    mcp_json.parent.mkdir(exist_ok=True)
    mcp_json.write_text(
        '{"mcpServers": {"echo": {"command": "true", "args": ["${MODEL_NAME}"]}}}',
        encoding="utf-8",
    )
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: first\n")
    runtime = GatewayRuntime()
    runtime.mcp = MCPClient()
    runtime.mcp.set_env_overlay(get_config_store().current().env_values)
    runtime.mcp.apply_catalog_diff = AsyncMock(  # type: ignore[method-assign]
        return_value=MCPCatalogApplyResult()
    )
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: second\n",
        encoding="utf-8",
    )
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert result.error is None
    runtime.mcp.apply_catalog_diff.assert_awaited()
    assert runtime.mcp._env_overlay is not None
    assert runtime.mcp._env_overlay["MODEL_NAME"] == "second"


@pytest.mark.asyncio
async def test_hot_reload_skips_mcp_when_json_has_no_env_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_json = tmp_path / "monkeybot_config" / "mcp.json"
    mcp_json.parent.mkdir(exist_ok=True)
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: first\n")
    runtime = GatewayRuntime()
    runtime.mcp = MCPClient()
    runtime.mcp.apply_catalog_diff = AsyncMock(  # type: ignore[method-assign]
        return_value=MCPCatalogApplyResult()
    )
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: second\n",
        encoding="utf-8",
    )
    await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    runtime.mcp.apply_catalog_diff.assert_not_called()


def test_relative_skills_path_env_resolves_against_agent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.gateway.sse.app import _resolved_workspace_paths

    agent = tmp_path / "agent"
    (agent / "monkeybot_config").mkdir(parents=True)
    (agent / "skills").mkdir()
    (agent / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("MONKEYBOT_CONFIG", str(agent / "monkeybot_config" / "monkeybot.yaml"))
    monkeypatch.setenv("SKILLS_PATH", "skills")
    monkeypatch.chdir(elsewhere)
    apply_monkeybot_runtime_env()
    _workspace, skills, _artifacts = _resolved_workspace_paths()
    assert skills == (agent / "skills").resolve()
    assert skills != (elsewhere / "skills").resolve()


@pytest.mark.asyncio
async def test_malformed_allowlist_reload_keeps_last_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    _write_allowlist(allow)
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    runtime = GatewayRuntime()
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())
    old_inspectors = runtime.inspectors
    assert any(isinstance(i, CommandTierInspector) for i in old_inspectors)

    allow.write_text("not: [valid: yaml", encoding="utf-8")
    yaml_path.write_text("model:\n  provider: fake\n  name: after\n", encoding="utf-8")
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert result.error is not None
    assert "inspector" in result.error.lower()
    assert runtime.inspectors is old_inspectors
    assert get_config_store().current().revision == 1


def test_relative_allowlist_resolves_against_agent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    allow = agent / "monkeybot_config" / "command_allowlist.yaml"
    _write_allowlist(allow)
    (agent / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("MONKEYBOT_CONFIG", str(agent / "monkeybot_config" / "monkeybot.yaml"))
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", "monkeybot_config/command_allowlist.yaml")
    monkeypatch.chdir(elsewhere)
    apply_monkeybot_runtime_env()
    runtime = GatewayRuntime()
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())
    assert any(isinstance(i, CommandTierInspector) for i in runtime.inspectors)


def test_relative_mcp_config_resolves_against_agent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    mcp = agent / "monkeybot_config" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    (agent / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("MONKEYBOT_CONFIG", str(agent / "monkeybot_config" / "monkeybot.yaml"))
    monkeypatch.setenv("MCP_CONFIG", "monkeybot_config/mcp.json")
    monkeypatch.chdir(elsewhere)
    apply_monkeybot_runtime_env()
    runtime = GatewayRuntime()
    resolved = runtime._mcp_config_path(
        get_config_store().current(), AgentLayout.from_environment()
    )
    assert resolved == mcp.resolve()
    assert resolved != (elsewhere / "monkeybot_config" / "mcp.json").resolve()


@pytest.mark.asyncio
async def test_failed_mcp_apply_restores_env_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_json = tmp_path / "monkeybot_config" / "mcp.json"
    mcp_json.parent.mkdir(exist_ok=True)
    mcp_json.write_text(
        '{"mcpServers": {"echo": {"command": "true", "args": ["${MODEL_NAME}"]}}}',
        encoding="utf-8",
    )
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: first\n")
    runtime = GatewayRuntime()
    runtime.mcp = MCPClient()
    runtime.mcp.set_env_overlay(get_config_store().current().env_values)
    runtime.mcp.apply_catalog_diff = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("catalog boom")
    )
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: second\n",
        encoding="utf-8",
    )
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert result.error is not None
    assert "catalog boom" in result.error
    assert runtime.mcp._env_overlay is not None
    assert runtime.mcp._env_overlay["MODEL_NAME"] == "first"
    assert get_config_store().current().revision == 1


@pytest.mark.asyncio
async def test_mcp_reload_times_out_while_turn_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("monkeybot.gateway.sse.app.IDLE_TURN_WAIT_SEC", 0.05)
    mcp_json = tmp_path / "monkeybot_config" / "mcp.json"
    mcp_json.parent.mkdir(exist_ok=True)
    mcp_json.write_text(
        '{"mcpServers": {"echo": {"command": "true", "args": ["${MODEL_NAME}"]}}}',
        encoding="utf-8",
    )
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: first\n")
    runtime = GatewayRuntime()
    runtime.mcp = MCPClient()
    runtime.mcp.apply_catalog_diff = AsyncMock(  # type: ignore[method-assign]
        return_value=MCPCatalogApplyResult()
    )
    begin_in_flight_turn()
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: second\n",
        encoding="utf-8",
    )
    _cfg, _diff, result = await _apply_reload(runtime, fastapi_app=_app_with_runtime(runtime))
    assert result.error is not None
    assert "timed out" in result.error.lower()
    runtime.mcp.apply_catalog_diff.assert_not_called()
    end_in_flight_turn()
