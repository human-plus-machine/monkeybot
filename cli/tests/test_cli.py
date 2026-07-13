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
    assert (tmp_path / "scripts" / "setup-workspace.sh").is_file()
    skills_link = tmp_path / "workspace" / "skills"
    if skills_link.is_symlink():
        assert skills_link.resolve() == (tmp_path / "skills").resolve()
    else:
        assert (tmp_path / "workspace" / "SKILLS_README.txt").is_file()
    text = (tmp_path / "monkeybot_config" / "monkeybot.yaml").read_text()
    assert "test-model" in text
    assert "workspace_root: ./workspace" in text
    env_text = (tmp_path / ".env.example").read_text()
    assert "MONKEYBOT_WORKSPACE_ROOT" not in env_text
    assert "AGENT_MD" not in env_text
    assert "MONKEYBOT_SUBAGENT_AGENT_MD" not in env_text
    assert "DB_URL" in env_text
    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "monkeybot[gemini]>=2.1.0,<3" in pyproject
    assert "[tool.uv.sources]" not in pyproject
    assert "uv sync" in result.stdout
    assert "pyproject.toml: created" in result.stdout


def test_new_force_reports_overwritten_config(tmp_path: Path) -> None:
    first = _run_cli("new", "--dest", str(tmp_path), "--yes")
    assert first.returncode == 0
    second = _run_cli("new", "--dest", str(tmp_path), "--yes", "--force")
    assert second.returncode == 0
    assert "monkeybot_config/monkeybot.yaml: overwritten" in second.stdout


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
