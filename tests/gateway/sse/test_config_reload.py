"""Tests for config hot-reload (turn lock, admin API, GatewayRuntime.apply)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from monkeybot.core.config import (
    apply_monkeybot_runtime_env,
    get_config_store,
    reset_runtime_env_state_for_tests,
)
from monkeybot.core.config.runtime_env import ENV_MAP, SUBAGENTS_DIFF_KEY
from monkeybot.core.config.snapshot import apply_reload_env_patch
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.gateway.sse.app import GatewayRuntime
from monkeybot.gateway.sse.reload import get_reload_lock, run_config_reload
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry


@pytest.fixture(autouse=True)
def _reset_reload_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    env_before = {k: os.environ.get(k) for k in ENV_MAP.values()}
    yield
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


@pytest.mark.asyncio
async def test_admin_reload_digest_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: stable\n")
    store = get_config_store()
    first = store.current()
    registry = SessionRegistry()
    app = create_app(registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == first.revision
    assert body["digest"] == first.digest
    assert body["changed"] == []
    assert body["restart_required"] == []
    assert body.get("error") is None
    assert store.current() is first


@pytest.mark.asyncio
async def test_admin_reload_reports_restart_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\npaths:\n  db_url: sqlite:///old.db\n",
    )
    yaml_path.write_text(
        "model:\n  provider: fake\npaths:\n  db_url: sqlite:///new.db\n",
        encoding="utf-8",
    )
    registry = SessionRegistry()
    app = create_app(registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 200
    body = r.json()
    assert "DB_URL" in body["restart_required"]
    assert body["revision"] == 2


@pytest.mark.asyncio
async def test_admin_get_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: flash\n")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/admin/config")
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 1
    assert body["env"]["MODEL_NAME"] == "flash"
    assert "digest" in body


@pytest.mark.asyncio
async def test_reload_preserves_session_bus_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: a\n")
    registry = SessionRegistry()
    bus = registry.create("s1", agent_md=None, created_at_ms=1, provider=object())
    bus.session_approvals.remember("run_command", "ls")
    approvals = bus.session_approvals
    override = bus.provider

    yaml_path.write_text("model:\n  provider: fake\n  name: b\n", encoding="utf-8")
    report = await run_config_reload(registry=registry, fastapi_app=None)
    assert "MODEL_NAME" in report.hot
    assert registry.get("s1") is bus
    assert bus.session_approvals is approvals
    assert bus.session_approvals.is_allowed("run_command", "ls")
    assert bus.provider is override
    assert bus.admission is bus.admission


@pytest.mark.asyncio
async def test_mid_turn_reload_keeps_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: first\n")
    store = get_config_store()
    pinned_rev: list[int] = []
    started = asyncio.Event()
    running_done = asyncio.Event()

    async def turn() -> None:
        async with get_reload_lock():
            pinned_rev.append(store.current().revision)
            started.set()
        await running_done.wait()
        pinned_rev.append(store.current().revision)

    async def reloader() -> None:
        await started.wait()
        yaml_path.write_text(
            "model:\n  provider: fake\n  name: second\n",
            encoding="utf-8",
        )
        await run_config_reload(registry=SessionRegistry(), fastapi_app=None)
        running_done.set()

    await asyncio.gather(turn(), reloader())
    assert pinned_rev[0] == 1
    assert pinned_rev[1] >= 2
    assert store.current().model.name == "second"


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
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert "MODEL_TEMPERATURE" in report.applied
    assert report.restart_required == []
    assert report.error is None
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
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(GatewayRuntime())
    )
    assert "SANDBOX_ENABLED" in report.changed
    assert "PENDING_RESPONSE_TIMEOUT_SEC" in report.changed
    assert "MONKEYBOT_SCHEDULER_ENABLED" in report.changed
    assert "SANDBOX_ENABLED" not in report.applied
    assert "PENDING_RESPONSE_TIMEOUT_SEC" not in report.applied
    assert "MONKEYBOT_SCHEDULER_ENABLED" not in report.applied


@pytest.mark.asyncio
async def test_rebuild_without_runtime_raises_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n  temperature: 0.1\n",
    )
    yaml_path.write_text(
        "model:\n  provider: fake\n  temperature: 0.9\n",
        encoding="utf-8",
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "GATEWAY_RUNTIME_NOT_BOUND"


@pytest.mark.asyncio
async def test_invalid_subagents_surfaces_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\nsubagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n",
    )
    runtime = GatewayRuntime()
    runtime.build_subagents()
    assert "helper" in runtime.subagent_registry
    yaml_path.write_text(
        "model:\n  provider: fake\nsubagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n"
        "    - name: helper\n      description: duplicate\n",
        encoding="utf-8",
    )
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is not None
    assert "Duplicate subagent" in report.error
    assert SUBAGENTS_DIFF_KEY not in report.applied
    assert "helper" in runtime.subagent_registry


@pytest.mark.asyncio
async def test_mcp_catalog_exception_surfaces_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_json = tmp_path / "monkeybot_config" / "mcp.json"
    mcp_json.parent.mkdir(exist_ok=True)
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    mcp_json.write_text(
        '{"mcpServers": {"srv": {"command": "true"}}}',
        encoding="utf-8",
    )
    runtime = GatewayRuntime()
    runtime.mcp = MCPClient()
    runtime.mcp.apply_catalog_diff = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("disconnect failed")
    )
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is not None
    assert "disconnect failed" in report.error
    assert "MCP_CONFIG" not in report.applied


@pytest.mark.asyncio
async def test_env_patch_computer_tools_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    applied = apply_reload_env_patch(
        {"MONKEYBOT_COMPUTER_TOOLS": "true", "DB_URL": "sqlite:///nope.db"}
    )
    assert applied == {"MONKEYBOT_COMPUTER_TOOLS": "true"}
    assert os.environ.get("MONKEYBOT_COMPUTER_TOOLS") == "true"

    registry = SessionRegistry()
    app = create_app(registry=registry)
    app.state.gateway_runtime = GatewayRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/admin/config/reload",
            json={"env": {"MONKEYBOT_TRANSCRIPT_ENABLED": "true"}},
        )
    assert r.status_code == 200
    assert os.environ.get("MONKEYBOT_TRANSCRIPT_ENABLED") == "true"


@pytest.mark.asyncio
async def test_reload_publishes_config_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: a\n")
    registry = SessionRegistry()
    bus = registry.create("s1", agent_md=None, created_at_ms=1)
    published: list[str] = []

    async def _capture(data_json: str, *, lane: str = "primary") -> int:
        del lane
        published.append(data_json)
        return 1

    bus.publish_data = _capture  # type: ignore[method-assign]
    yaml_path.write_text("model:\n  provider: fake\n  name: b\n", encoding="utf-8")
    await run_config_reload(registry=registry, fastapi_app=None)
    assert any('"type":"ConfigReloaded"' in item for item in published)
