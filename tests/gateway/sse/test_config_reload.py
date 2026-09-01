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
from monkeybot.core.config.runtime_env import ENV_MAP
from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.snapshot import apply_reload_env_patch
from monkeybot.core.layout import AgentLayout
from monkeybot.core.mcp.mcp_client import MCPCatalogApplyResult, MCPClient
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.inspector import CommandTierConfigError, CommandTierInspector
from monkeybot.gateway.realtime.deps import RealtimeDependencies
from monkeybot.gateway.sse.app import (
    _LIVE_SLICE_ATTRS,
    _RESTART_ONLY_ATTRS,
    GatewayRuntime,
)
from monkeybot.gateway.sse.models import APIError
from monkeybot.gateway.sse.reload import (
    begin_in_flight_turn,
    end_in_flight_turn,
    get_reload_lock,
    run_config_reload,
)
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry


@pytest.fixture(autouse=True)
def _reset_reload_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    env_before = {k: os.environ.get(k) for k in ENV_MAP.values()}
    yield
    from monkeybot.gateway.sse import reload as reload_mod

    reload_mod._in_flight_turns = 0
    reload_mod._turns_idle.set()
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


def _loopback_transport(app: FastAPI, *, host: str = "127.0.0.1") -> ASGITransport:
    return ASGITransport(app=app, client=(host, 0))


@pytest.mark.asyncio
async def test_admin_reload_digest_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: stable\n")
    store = get_config_store()
    first = store.current()
    registry = SessionRegistry()
    app = create_app(registry=registry)
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
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
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 200
    body = r.json()
    assert "DB_URL" in body["restart_required"]
    assert body["revision"] == 2


@pytest.mark.asyncio
async def test_admin_get_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n  name: flash\n")
    app = create_app()
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
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
async def test_reload_syncs_realtime_live_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n  temperature: 0.1\n",
    )
    runtime = GatewayRuntime()
    runtime.build_provider(get_config_store().current())
    runtime.inspectors = ["fresh-inspector"]  # type: ignore[list-item]
    runtime.hook_manager = "fresh-hooks"  # type: ignore[assignment]

    deps = RealtimeDependencies()
    sentinel_storage = object()
    deps.storage = sentinel_storage  # type: ignore[assignment]
    deps.realtime_provider = object()  # type: ignore[assignment]
    deps.inspectors = ["stale"]
    deps.freeze()

    rebuilt = object()
    monkeypatch.setattr(
        "monkeybot.gateway.realtime.deps.GeminiLiveProvider",
        lambda *args, **kwargs: rebuilt,
    )

    app = _app_with_runtime(runtime)
    app.state.realtime_deps = deps

    yaml_path.write_text(
        "model:\n  provider: fake\n  temperature: 0.9\n",
        encoding="utf-8",
    )
    report = await run_config_reload(registry=SessionRegistry(), fastapi_app=app)
    assert report.error is None
    assert deps.inspectors == ["fresh-inspector"]
    assert deps.hook_manager == "fresh-hooks"
    assert deps.realtime_provider is rebuilt
    assert deps.storage is sentinel_storage


@pytest.mark.asyncio
async def test_failed_apply_does_not_sync_realtime_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    _write_allowlist(allow)
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n  temperature: 0.1\n",
    )
    runtime = GatewayRuntime()
    runtime.build_provider(get_config_store().current())
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())

    deps = RealtimeDependencies()
    old_provider = object()
    deps.realtime_provider = old_provider  # type: ignore[assignment]
    deps.inspectors = ["stale"]
    deps.freeze()

    app = _app_with_runtime(runtime)
    app.state.realtime_deps = deps

    allow.write_text("not: [valid: yaml", encoding="utf-8")
    yaml_path.write_text(
        "model:\n  provider: fake\n  temperature: 0.9\n",
        encoding="utf-8",
    )
    report = await run_config_reload(registry=SessionRegistry(), fastapi_app=app)
    assert report.error is not None
    assert deps.inspectors == ["stale"]
    assert deps.realtime_provider is old_provider


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
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "GATEWAY_RUNTIME_NOT_BOUND"
    assert get_config_store().current().model.temperature == "0.1"
    assert get_config_store().current().revision == 1


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
    with pytest.raises(APIError) as exc_info:
        await run_config_reload(registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime))
    assert exc_info.value.status_code == 400
    assert "Duplicate subagent" in exc_info.value.message
    assert "helper" in runtime.subagent_registry
    assert get_config_store().current().revision == 1


@pytest.mark.asyncio
async def test_admin_reload_invalid_config_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\nsubagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n",
    )
    yaml_path.write_text(
        "model:\n  provider: fake\nsubagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n"
        "    - name: helper\n      description: duplicate\n",
        encoding="utf-8",
    )
    app = create_app()
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CONFIG"
    assert "Duplicate subagent" in r.json()["error"]["message"]


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
    assert get_config_store().current().revision == 1


@pytest.mark.asyncio
async def test_admin_reload_apply_failure_returns_500(
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
    app = create_app()
    app.state.gateway_runtime = runtime
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "RELOAD_APPLY_FAILED"
    assert "disconnect failed" in body["error"]["message"]
    assert get_config_store().current().revision == 1


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
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        r = await client.post(
            "/admin/config/reload",
            json={"env": {"MONKEYBOT_TRANSCRIPT_ENABLED": "true"}},
        )
    assert r.status_code == 200
    # YAML-only: reload must not advertise or apply the retired env pin.
    assert os.environ.get("MONKEYBOT_TRANSCRIPT_ENABLED") is None


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


@pytest.mark.asyncio
async def test_admin_get_config_redacts_db_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DB_URL", raising=False)
    _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n"
        "paths:\n  db_url: postgresql://user:secret@db.example/monkeybot\n",
    )
    app = create_app()
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        r = await client.get("/admin/config")
    assert r.status_code == 200
    body = r.json()
    assert body["env"]["DB_URL"] == "***"
    assert "secret" not in r.text


def test_redact_env_fails_closed_on_secret_names() -> None:
    from monkeybot.gateway.sse.reload import _redact_env

    out = _redact_env(
        {
            "MODEL_NAME": "flash",
            "OPENAI_API_KEY": "sk-secret",
            "MONKEYBOT_ADMIN_TOKEN": "s3cret",
            "DB_PASSWORD": "hunter2",
            "DB_URL": "sqlite:///local.db",
            "MONKEYBOT_SCHEDULER_ENABLED": "true",
            "MODEL_MAX_TOKENS": "8192",
        }
    )
    assert out["MODEL_NAME"] == "flash"
    assert out["OPENAI_API_KEY"] == "***"
    assert out["MONKEYBOT_ADMIN_TOKEN"] == "***"
    assert out["DB_PASSWORD"] == "***"
    assert out["DB_URL"] == "***"
    assert out["MONKEYBOT_SCHEDULER_ENABLED"] == "true"
    assert out["MODEL_MAX_TOKENS"] == "8192"


@pytest.mark.asyncio
async def test_admin_rejects_non_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("10.1.2.3", 9)),
        base_url="http://test",
    ) as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_rejects_testclient_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("testclient", 0)),
        base_url="http://test",
    ) as client:
        r = await client.post("/admin/config/reload", json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_token_required_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_ADMIN_TOKEN", "s3cret")
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    app = create_app()
    async with AsyncClient(transport=_loopback_transport(app), base_url="http://test") as client:
        denied = await client.post("/admin/config/reload", json={})
        assert denied.status_code == 401
        ok = await client.post(
            "/admin/config/reload",
            json={},
            headers={"Authorization": "Bearer s3cret"},
        )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_admin_token_does_not_bypass_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid bearer token must not exempt a remote client from loopback-only."""
    monkeypatch.setenv("MONKEYBOT_ADMIN_TOKEN", "s3cret")
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("10.1.2.3", 9)),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/admin/config/reload",
            json={},
            headers={"Authorization": "Bearer s3cret"},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_failed_reload_rolls_back_staged_env_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patch applied before a failed apply must not outlive the failed reload."""
    monkeypatch.delenv("MONKEYBOT_COMPUTER_TOOLS", raising=False)
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n    - name: helper\n      description: helps\n",
    )
    runtime = GatewayRuntime()
    runtime.build_provider(get_config_store().current())
    runtime.build_subagents()
    yaml_path.write_text(
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n"
        "    - name: helper\n      description: duplicate\n",
        encoding="utf-8",
    )
    with pytest.raises(APIError) as exc_info:
        await run_config_reload(
            registry=SessionRegistry(),
            fastapi_app=_app_with_runtime(runtime),
            env={"MONKEYBOT_COMPUTER_TOOLS": "true"},
        )
    assert exc_info.value.status_code == 400
    assert "Duplicate subagent" in exc_info.value.message
    # The staged patch must not survive a reload that never committed.
    assert os.environ.get("MONKEYBOT_COMPUTER_TOOLS") is None
    assert get_config_store().current().revision == 1
    assert get_config_store().current().env_values.get("MONKEYBOT_COMPUTER_TOOLS") != "true"


@pytest.mark.asyncio
async def test_first_load_env_patch_keeps_operator_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config.snapshot import pinned_env_names

    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path, "model:\n  provider: fake\n  name: yaml-name\n")
    monkeypatch.setenv("MODEL_NAME", "from-env")
    monkeypatch.delenv("MONKEYBOT_TRANSCRIPT_ENABLED", raising=False)
    reset_runtime_env_state_for_tests()
    report = await run_config_reload(
        registry=SessionRegistry(),
        fastapi_app=None,
        env={"MONKEYBOT_TRANSCRIPT_ENABLED": "true"},
    )
    assert report.error is None
    assert "MODEL_NAME" in pinned_env_names()
    assert get_config_store().current().model.name == "from-env"
    assert os.environ.get("MONKEYBOT_TRANSCRIPT_ENABLED") is None


@pytest.mark.asyncio
async def test_missing_allowlist_reload_rejects_when_policy_was_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an active allowlist on reload must not fall open to allow-all."""
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
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is not None
    assert "command tier config missing" in report.error
    assert runtime.inspectors is old_inspectors
    assert runtime.run_command_allowed_commands is old_allowed
    assert get_config_store().current().revision == 1


def test_missing_allowlist_at_fresh_startup_allows_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing allowlist at first boot (no prior policy) still falls open."""
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    runtime = GatewayRuntime()
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())
    assert not any(isinstance(i, CommandTierInspector) for i in runtime.inspectors)
    assert runtime.run_command_allowed_commands is None


@pytest.mark.asyncio
async def test_skills_path_hot_reload_updates_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKILLS_PATH", raising=False)
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\npaths:\n  skills_path: skills-a\n",
    )
    yaml_path.write_text(
        "model:\n  provider: fake\npaths:\n  skills_path: skills-b\n",
        encoding="utf-8",
    )
    await run_config_reload(registry=SessionRegistry(), fastapi_app=None)
    assert os.environ["SKILLS_PATH"].endswith("skills-b")
    assert get_config_store().current().paths.skills_path is not None
    assert get_config_store().current().paths.skills_path.endswith("skills-b")


@pytest.mark.asyncio
async def test_failed_apply_leaves_live_slices_on_old_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    _write_allowlist(allow)
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n  temperature: 0.1\n",
    )
    runtime = GatewayRuntime()
    runtime.build_provider(get_config_store().current())
    runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())
    old_provider = runtime.provider
    old_inspectors = runtime.inspectors
    allow.write_text("not: [valid: yaml", encoding="utf-8")
    yaml_path.write_text(
        "model:\n  provider: fake\n  temperature: 0.9\n",
        encoding="utf-8",
    )
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is not None
    assert "inspector" in report.error.lower()
    assert report.applied == []
    assert runtime.provider is old_provider
    assert runtime.inspectors is old_inspectors
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
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is None
    runtime.mcp.apply_catalog_diff.assert_awaited()
    assert runtime.mcp.env_overlay is not None
    assert runtime.mcp.env_overlay["MODEL_NAME"] == "second"


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
    await run_config_reload(registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime))
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


def _write_allowlist(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "allowed_commands:\n  - ls\nallowed_path_prefixes:\n  - ./\n",
        encoding="utf-8",
    )


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
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is not None
    assert "inspector" in report.error.lower()
    assert report.applied == []
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
    report = await run_config_reload(
        registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
    )
    assert report.error is not None
    assert "catalog boom" in report.error
    assert runtime.mcp.env_overlay is not None
    assert runtime.mcp.env_overlay["MODEL_NAME"] == "first"
    assert get_config_store().current().revision == 1


@pytest.mark.asyncio
async def test_mcp_reload_times_out_while_turn_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("monkeybot.gateway.sse.reload.IDLE_TURN_WAIT_SEC", 0.05)
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
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: second\n",
        encoding="utf-8",
    )
    begin_in_flight_turn()
    try:
        report = await run_config_reload(
            registry=SessionRegistry(), fastapi_app=_app_with_runtime(runtime)
        )
    finally:
        end_in_flight_turn()
    assert report.error is not None
    assert "timed out" in report.error.lower()
    assert report.applied == []
    runtime.mcp.apply_catalog_diff.assert_not_called()
    assert get_config_store().current().revision == 1


def test_malformed_allowlist_at_fresh_startup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken allowlist is not 'missing' — boot must not silently fall open to allow-all."""
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    allow.parent.mkdir(parents=True, exist_ok=True)
    allow.write_text("not: [valid: yaml", encoding="utf-8")
    _boot_fake(tmp_path, monkeypatch, "model:\n  provider: fake\n")
    runtime = GatewayRuntime()
    with pytest.raises(CommandTierConfigError):
        runtime.build_inspectors(AgentLayout.from_environment(), get_config_store().current())


def test_live_slice_attrs_cover_all_non_restart_fields() -> None:
    from dataclasses import fields

    names = {f.name for f in fields(GatewayRuntime)}
    assert set(_LIVE_SLICE_ATTRS) == names - _RESTART_ONLY_ATTRS
    assert "mcp" not in _LIVE_SLICE_ATTRS
    assert "inspectors" in _LIVE_SLICE_ATTRS
    assert "subagent_registry" in _LIVE_SLICE_ATTRS


def test_end_in_flight_turn_without_begin_raises() -> None:
    from monkeybot.gateway.sse import reload as reload_mod

    reload_mod._in_flight_turns = 0
    reload_mod._turns_idle.set()
    with pytest.raises(RuntimeError, match="without matching begin_in_flight_turn"):
        end_in_flight_turn()


@pytest.mark.asyncio
async def test_subagents_apply_uses_pinned_snapshot_not_later_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n    - name: helper\n      description: helps\n",
    )
    runtime = GatewayRuntime()
    runtime.build_subagents(get_config_store().current())
    assert set(runtime.subagent_registry) == {"helper"}

    yaml_path.write_text(
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n"
        "    - name: researcher\n      description: researches\n",
        encoding="utf-8",
    )
    store = get_config_store()
    cfg, diff = store.prepare_reload()
    assert not diff.noop
    yaml_path.write_text(
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n"
        "    - name: helper\n      description: helps\n"
        "    - name: sneaky\n      description: third write\n",
        encoding="utf-8",
    )
    result = await runtime.apply(cfg, diff, fastapi_app=_app_with_runtime(runtime))
    assert result.error is None
    assert "researcher" in runtime.subagent_registry
    assert "sneaky" not in runtime.subagent_registry
    store.commit(cfg)


@pytest.mark.asyncio
async def test_duplicate_subagent_name_rejected_on_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n    - name: helper\n      description: helps\n",
    )
    runtime = GatewayRuntime()
    runtime.build_subagents(get_config_store().current())
    old_registry = runtime.subagent_registry
    yaml_path.write_text(
        "model:\n  provider: fake\n"
        "subagents:\n  personas:\n"
        "    - name: helper\n      description: first\n"
        "    - name: helper\n      description: SECOND-WINS\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Duplicate subagent name"):
        get_config_store().prepare_reload()
    assert runtime.subagent_registry is old_registry
    assert runtime.subagent_registry["helper"].description == "helps"
    assert get_config_store().current().revision == 1
    assert get_config_store().current().subagents["helper"].description == "helps"


@pytest.mark.asyncio
async def test_subagent_config_error_skips_memory_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _boot_fake(
        tmp_path,
        monkeypatch,
        "model:\n  provider: fake\nmemory:\n  enabled: true\n"
        "subagents:\n  personas:\n    - name: helper\n      description: helps\n",
    )
    runtime = GatewayRuntime()
    runtime.build_subagents(get_config_store().current())
    app = _app_with_runtime(runtime)
    app.state.memory = "keep-me"
    app.state.memory_status = "enabled"
    app.state.memory_detail = None

    def _boom(self: GatewayRuntime, cfg: object = None) -> None:
        del self, cfg
        raise ConfigError("invalid subagents")

    monkeypatch.setattr(GatewayRuntime, "build_subagents", _boom)
    yaml_path.write_text(
        "model:\n  provider: fake\nmemory:\n  enabled: false\n"
        "subagents:\n  personas:\n    - name: other\n      description: other\n",
        encoding="utf-8",
    )
    report = await run_config_reload(registry=SessionRegistry(), fastapi_app=app)
    assert report.error == "invalid subagents"
    assert report.applied == []
    assert app.state.memory == "keep-me"
    assert app.state.memory_status == "enabled"
