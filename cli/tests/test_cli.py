"""CLI tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "monkeybot_cli.main", *args],
        cwd=cwd or CLI_ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(CLI_ROOT / "src")},
    )


def test_new_scaffolds(tmp_path: Path) -> None:
    result = _run_cli("new", "--dest", str(tmp_path), "--yes", "--provider", "gemini", "--model", "test-model")
    assert result.returncode == 0
    assert (tmp_path / "monkeybot_config" / "monkeybot.yaml").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / "workspace" / ".gitkeep").is_file()
    assert not (tmp_path / "workspace" / "skills").exists()
    assert (tmp_path / "skills").is_dir()
    assert not (tmp_path / "skills" / "browser").exists()
    assert not (tmp_path / "skills" / "image-generator").exists()
    assert not (tmp_path / "skills" / "loop").exists()
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / ".dockerignore").is_file()
    text = (tmp_path / "monkeybot_config" / "monkeybot.yaml").read_text()
    assert "test-model" in text
    assert "workspace_root: ./workspace" in text
    assert "localhost:18080" in text
    env_text = (tmp_path / ".env.example").read_text()
    assert "MONKEYBOT_WORKSPACE_ROOT" not in env_text
    assert "AGENT_MD" not in env_text
    assert "MONKEYBOT_SUBAGENT_AGENT_MD" not in env_text
    assert "DB_URL" in env_text
    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "monkeybot[gemini,sandbox,web-search,memory]>=3.0.0,<4" in pyproject
    assert "monkeybot-browser-mcp>=0.2.0,<1" in pyproject
    assert "package = false" in pyproject
    assert "[tool.uv.sources]" not in pyproject
    assert "uv sync" in result.stdout
    assert "pyproject.toml: created" in result.stdout


def test_new_force_reports_overwritten_config(tmp_path: Path) -> None:
    first = _run_cli("new", "--dest", str(tmp_path), "--yes")
    assert first.returncode == 0
    second = _run_cli("new", "--dest", str(tmp_path), "--yes", "--force")
    assert second.returncode == 0
    assert "monkeybot_config/monkeybot.yaml: overwritten" in second.stdout


def test_refresh_updates_existing_agent(tmp_path: Path) -> None:
    created = _run_cli("new", "--dest", str(tmp_path), "--yes")
    assert created.returncode == 0
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    allow.write_text("allowed_commands:\n  - bash\n  - officecli\n", encoding="utf-8")
    original_pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    result = _run_cli("refresh", "--dest", str(tmp_path))
    assert result.returncode == 0, result.stderr
    text = allow.read_text(encoding="utf-8")
    assert "mempalace" in text
    assert "officecli" in text
    assert "command_allowlist.yaml: updated" in result.stdout
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original_pyproject


def test_refresh_rejects_empty_dest(tmp_path: Path) -> None:
    result = _run_cli("refresh", "--dest", str(tmp_path))
    assert result.returncode == 2
    assert "not a scaffolded agent" in result.stderr


def test_run_fail_closed_when_venv_cannot_import_harness(tmp_path: Path) -> None:
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n  name: fake\n",
        encoding="utf-8",
    )
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    py.chmod(0o755)
    result = _run_cli("run", "--cwd", str(tmp_path))
    assert result.returncode == 2
    assert "refusing to start" in result.stderr
    assert "pyproject.toml" not in result.stderr


def test_write_active_config_reports_overwritten_on_force(tmp_path: Path) -> None:
    from monkeybot_cli.scaffold import write_active_config

    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    active = cfg_dir / "monkeybot.yaml"
    active.write_text("old: true\n", encoding="utf-8")

    status = write_active_config(cfg_dir, provider=None, model=None, force=True)

    assert status == "overwritten"
    assert "old: true" not in active.read_text()


def test_write_active_config_reports_created(tmp_path: Path) -> None:
    from monkeybot_cli.scaffold import write_active_config

    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()

    status = write_active_config(cfg_dir, provider="gemini", model="test-model", force=False)

    assert status == "created"
    text = (cfg_dir / "monkeybot.yaml").read_text()
    assert "test-model" in text


def test_validate_missing_config(tmp_path: Path) -> None:
    result = _run_cli("validate", "--json", "--cwd", str(tmp_path))
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert any(c["id"] == "config.file.exists" for c in data["checks"])


def test_validate_custom_config_anchors_paths_at_its_parent(tmp_path: Path) -> None:
    config_dir = tmp_path / "custom-config"
    config_dir.mkdir()
    (config_dir / "AGENT.md").write_text("# Agent\n", encoding="utf-8")
    (config_dir / "skills").mkdir()
    config = config_dir / "agent.yaml"
    config.write_text(
        "model:\n"
        "  provider: fake\n"
        "  name: fake\n"
        "paths:\n"
        "  agent_md: AGENT.md\n"
        "  skills_path: skills\n"
        "  db_url: sqlite:///data/monkeybot.db\n"
        "  memory_storage_uri: local://memory\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    result = _run_cli("validate", "--json", "--config", str(config), cwd=unrelated)

    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["paths.agent_md.exists"]["status"] == "pass"
    assert checks["paths.skills_path.exists"]["status"] == "pass"
    assert checks["memory.backend.supported"]["status"] == "pass"


def test_validate_rejects_object_store_memory_uri(tmp_path: Path) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "AGENT.md").write_text("# Agent\n", encoding="utf-8")
    (config_dir / "skills").mkdir()
    (config_dir / "monkeybot.yaml").write_text(
        "model:\n"
        "  provider: fake\n"
        "  name: fake\n"
        "paths:\n"
        "  agent_md: AGENT.md\n"
        "  skills_path: skills\n"
        "  db_url: sqlite:///data/monkeybot.db\n"
        "  memory_storage_uri: gcs://bucket/prefix\n",
        encoding="utf-8",
    )
    result = _run_cli("validate", "--json", "--cwd", str(tmp_path))
    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["memory.backend.supported"]["status"] == "fail"
    assert "local://" in checks["memory.backend.supported"]["message"]


def test_talk_help_lists_realtime_flags() -> None:
    result = _run_cli("talk", "--help")
    assert result.returncode == 0
    assert "--gateway-url" in result.stdout
    assert "--text" in result.stdout
    assert "8080" in result.stdout


def test_load_agent_dotenv_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(CLI_ROOT)
    (tmp_path / ".env").write_text("GOOGLE_CLOUD_PROJECT=cli-test-project\n")
    (tmp_path / "monkeybot_config").mkdir()
    (tmp_path / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  provider: vertex\n  name: test\nruntime:\n  port: 9999\n"
    )
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    from monkeybot_cli.config_resolve import load_agent_dotenv

    loaded = load_agent_dotenv(cwd=tmp_path)
    assert loaded == tmp_path / ".env"
    assert __import__("os").environ.get("GOOGLE_CLOUD_PROJECT") == "cli-test-project"


def test_doctor_loads_agent_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(CLI_ROOT)
    (tmp_path / ".env").write_text("GOOGLE_CLOUD_PROJECT=doctor-test-project\n")
    (tmp_path / "monkeybot_config").mkdir()
    (tmp_path / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  provider: vertex\n  name: test\nruntime:\n  port: 9999\n"
    )
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    result = _run_cli("doctor", "--json", "--cwd", str(tmp_path))
    data = json.loads(result.stdout)
    cred = next(c for c in data["checks"] if c["id"] == "provider.credentials.present")
    assert cred["status"] == "pass"


def test_doctor_reports_layout_browser_sandbox_and_legacy_migration(tmp_path: Path) -> None:
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n  name: fake\npaths:\n  workspace_root: ./workspace\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "existing.md").write_text("trusted skill\n", encoding="utf-8")
    (cfg / "mcp.json").write_text('{"mcpServers": {"browser": {"enabled": false}}}', encoding="utf-8")

    result = _run_cli("doctor", "--json", "--cwd", str(tmp_path))
    assert result.returncode == 0, result.stderr
    checks = {item["id"]: item for item in json.loads(result.stdout)["checks"]}
    assert checks["layout.resolved"]["status"] == "pass"
    assert checks["layout.resolved"]["value"]["workspace"] == str(workspace.resolve()), result.stdout
    assert checks["layout.legacy_nested_skills"]["status"] == "fail"
    assert checks["layout.legacy_nested_skills"]["value"] == {
        "source": str((workspace / "skills").resolve()),
        "destination": str((tmp_path / "skills").resolve()),
        "collision": True,
        "action": None,
    }
    assert "do not move automatically" in checks["layout.legacy_nested_skills"]["remediation"]
    assert "disabled (bundled)" in checks["browser.bundled"]["message"]
    assert checks["sandbox.status"]["status"] == "pass"
    assert checks["env.harness.compatible"]["status"] == "pass"
