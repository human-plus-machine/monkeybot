"""Tests for doctor remediation copy."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from monkeybot_cli.commands.doctor import (
    _agent_defines_project_extra,
    _extra_remediation,
    _runtime_python_version,
)
from monkeybot_cli.runtime_python import RuntimePython


def test_extra_remediation_default_mvp_wording(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "bot"\ndependencies = ["monkeybot>=2.1.0,<3"]\n',
        encoding="utf-8",
    )
    runtime = SimpleNamespace(source="venv")
    text = _extra_remediation("openai", tmp_path, runtime)
    assert "uv sync --extra" not in text
    assert f"Add monkeybot[openai] to {tmp_path}/pyproject.toml dependencies" in text
    assert f"cd {tmp_path} && uv sync" in text


def test_extra_remediation_keeps_extra_for_project_optionals(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "bot"\n'
        "[project.optional-dependencies]\n"
        'openai = ["monkeybot[openai]"]\n',
        encoding="utf-8",
    )
    runtime = SimpleNamespace(source="uv")
    text = _extra_remediation("openai", tmp_path, runtime)
    assert text == f"Install in the agent project: cd {tmp_path} && uv sync --extra openai"


def test_extra_remediation_config_only_cli_env(tmp_path: Path) -> None:
    runtime = SimpleNamespace(source="cli")
    text = _extra_remediation("bedrock", tmp_path, runtime)
    assert "uv tool install --with 'monkeybot[bedrock]' monkeybot-cli" in text


def test_agent_defines_project_extra(tmp_path: Path) -> None:
    assert not _agent_defines_project_extra(tmp_path, "openai")
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\npostgres = []\n",
        encoding="utf-8",
    )
    assert _agent_defines_project_extra(tmp_path, "postgres")
    assert not _agent_defines_project_extra(tmp_path, "openai")


def test_runtime_python_version_returns_zeros_when_uv_missing(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = RuntimePython(["uv", "run", "python"], "uv", tmp_path)

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("uv")

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert _runtime_python_version(runtime, tmp_path) == (0, 0, 0)


def test_runtime_python_version_returns_zeros_on_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = RuntimePython(["uv", "run", "python"], "uv", tmp_path)

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="uv", timeout=15)

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert _runtime_python_version(runtime, tmp_path) == (0, 0, 0)
