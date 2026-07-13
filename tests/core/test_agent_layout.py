"""Contract tests for canonical agent-root path resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from monkeybot.core.config import runtime_env
from monkeybot.core.layout import bootstrap_agent_layout
from monkeybot.gateway.bootstrap import log_gateway_startup


def _clear_layout_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MONKEYBOT_AGENT_ROOT",
        "MONKEYBOT_CONFIG",
        "MONKEYBOT_WORKSPACE_ROOT",
        "WORKSPACE_ROOT",
        "SKILLS_PATH",
        "AGENT_MD",
        "MCP_CONFIG",
        "COMMAND_ALLOWLIST_CONFIG",
        "PERMISSION_CONFIG",
        "DB_URL",
        "MEMORY_STORAGE_URI",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_agent(root: Path) -> Path:
    config = root / "monkeybot_config"
    config.mkdir(parents=True)
    path = config / "monkeybot.yaml"
    path.write_text(
        "paths:\n"
        "  workspace_root: ./workspace\n"
        "  skills_path: ./skills\n"
        "  agent_md: ./monkeybot_config/AGENT.md\n"
        "  db_url: sqlite:///data/monkeybot.db\n"
        "  memory_storage_uri: local://./data/memory\n"
        "  mcp_config: ./monkeybot_config/mcp.json\n"
        "  command_allowlist_config: ./monkeybot_config/command_allowlist.yaml\n"
        "  permission_config: ./monkeybot_config/permissions.yaml\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("MODEL_NAME=from-root-dotenv\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("launch_place", ["root", "nested", "unrelated"])
def test_bootstrap_layout_is_identical_from_every_launch_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_place: str
) -> None:
    agent = tmp_path / "agent"
    config = _write_agent(agent)
    nested = agent / "workspace" / "deep"
    nested.mkdir(parents=True)
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    cwd = {"root": agent, "nested": nested, "unrelated": unrelated}[launch_place]
    before = dict(os.environ)
    try:
        runtime_env.reset_runtime_env_state_for_tests()
        _clear_layout_overrides(monkeypatch)
        monkeypatch.chdir(cwd)
        if launch_place == "unrelated":
            monkeypatch.setenv("MONKEYBOT_CONFIG", str(config))

        layout = bootstrap_agent_layout()

        assert layout.agent_root == agent.resolve()
        assert layout.workspace_root == (agent / "workspace").resolve()
        assert layout.skills_path == (agent / "skills").resolve()
        assert layout.db_url == f"sqlite:///{(agent / 'data' / 'monkeybot.db').resolve()}"
        assert layout.memory_storage_uri == f"local://{(agent / 'data' / 'memory').resolve()}"
        assert os.environ["MODEL_NAME"] == "from-root-dotenv"
        assert os.environ["MONKEYBOT_WORKSPACE_ROOT"] == str((agent / "workspace").resolve())

        # Every launch surface must agree with the canonical layout after the
        # same bootstrap, regardless of whether it started at root, a nested
        # workspace path, or an unrelated launcher cwd with MONKEYBOT_CONFIG.
        from monkeybot.core.subagents import subagent_worker
        from monkeybot.gateway.realtime.routes import _resolved_workspace_paths as realtime_paths
        from monkeybot.gateway.sse.app import _resolved_workspace_paths as sse_paths
        from monkeybot_cli.config_resolve import resolve_agent_root as cli_agent_root

        assert sse_paths() == (layout.workspace_root, layout.skills_path)
        assert realtime_paths() == (layout.workspace_root, layout.skills_path)
        assert cli_agent_root() == layout.agent_root
        assert subagent_worker.resolve_agent_project_root() == layout.agent_root
    finally:
        os.environ.clear()
        os.environ.update(before)
        runtime_env.reset_runtime_env_state_for_tests()


def test_process_env_path_override_remains_authoritative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "agent"
    _write_agent(agent)
    custom = tmp_path / "mounted-workspace"
    before = dict(os.environ)
    try:
        runtime_env.reset_runtime_env_state_for_tests()
        _clear_layout_overrides(monkeypatch)
        monkeypatch.chdir(agent)
        monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(custom))
        layout = bootstrap_agent_layout()
        assert layout.workspace_root == custom.resolve()
    finally:
        os.environ.clear()
        os.environ.update(before)
        runtime_env.reset_runtime_env_state_for_tests()


def test_legacy_workspace_root_override_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "agent"
    _write_agent(agent)
    custom = tmp_path / "legacy-mounted-workspace"
    before = dict(os.environ)
    try:
        runtime_env.reset_runtime_env_state_for_tests()
        _clear_layout_overrides(monkeypatch)
        monkeypatch.chdir(agent)
        monkeypatch.setenv("WORKSPACE_ROOT", str(custom))

        layout = bootstrap_agent_layout()

        assert layout.workspace_root == custom.resolve()
        assert os.environ["MONKEYBOT_WORKSPACE_ROOT"] == str(custom.resolve())
    finally:
        os.environ.clear()
        os.environ.update(before)
        runtime_env.reset_runtime_env_state_for_tests()


def test_startup_layout_log_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = tmp_path / "agent"
    _write_agent(agent)
    secret = "this-must-not-appear"
    before = dict(os.environ)
    try:
        runtime_env.reset_runtime_env_state_for_tests()
        _clear_layout_overrides(monkeypatch)
        monkeypatch.chdir(agent)
        monkeypatch.setenv("DB_URL", f"postgresql://user:{secret}@db.example/monkeybot")
        monkeypatch.setenv("SANDBOX_API_KEY", secret)
        layout = bootstrap_agent_layout()
        with caplog.at_level("INFO"):
            log_gateway_startup(layout, force=True)
        message = "\n".join(caplog.messages)
        assert "gateway_startup_layout" in message
        assert "db.example" in message
        assert secret not in message
        assert "user:" not in message
        assert "agent_root=" not in message
    finally:
        os.environ.clear()
        os.environ.update(before)
        runtime_env.reset_runtime_env_state_for_tests()
