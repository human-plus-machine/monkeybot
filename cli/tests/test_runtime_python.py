"""Tests for runtime_python resolution."""

from __future__ import annotations

import sys
from pathlib import Path

from monkeybot_cli.runtime_python import (
    RuntimePython,
    gateway_argv,
    prepare_runtime_python,
    resolve_runtime_python,
    run_probe,
)


def test_uses_venv_python_when_present(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    runtime = resolve_runtime_python(tmp_path)

    assert runtime.source == "venv"
    assert runtime.argv == [str(py)]
    assert gateway_argv(runtime) == [str(py), "-m", "monkeybot.gateway.realtime_main"]


def test_falls_back_to_uv_run_when_pyproject_present(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")

    runtime = resolve_runtime_python(tmp_path)

    assert runtime.source == "uv"
    assert runtime.argv == ["uv", "run", "python"]
    assert gateway_argv(runtime) == [
        "uv",
        "run",
        "python",
        "-m",
        "monkeybot.gateway.realtime_main",
    ]


def test_falls_back_to_cli_executable_for_config_only_tree(tmp_path: Path) -> None:
    # No .venv, no pyproject.toml — legacy monkeybot_config/-only agent.
    runtime = resolve_runtime_python(tmp_path)

    assert runtime.source == "cli"
    assert runtime.argv == [sys.executable]
    assert gateway_argv(runtime) == [
        sys.executable,
        "-m",
        "monkeybot.gateway.realtime_main",
    ]


def test_gateway_argv_sse_only_module(tmp_path: Path) -> None:
    from monkeybot_cli.runtime_python import SSE_GATEWAY_MODULE

    runtime = resolve_runtime_python(tmp_path)
    assert gateway_argv(runtime, module=SSE_GATEWAY_MODULE) == [
        sys.executable,
        "-m",
        "monkeybot.gateway.main",
    ]

def test_venv_takes_precedence_over_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    runtime = resolve_runtime_python(tmp_path)

    assert runtime.source == "venv"
    assert runtime.argv == [str(py)]


def test_run_probe_executes_code_under_runtime() -> None:
    runtime = RuntimePython([sys.executable], "cli")
    assert run_probe(runtime, "import sys") is True
    assert run_probe(runtime, "import sys; sys.exit(1)") is False
    assert run_probe(runtime, "import this_module_does_not_exist_xyz") is False


def test_run_probe_uv_sets_agent_root_cwd(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    runtime = resolve_runtime_python(tmp_path)
    assert runtime.source == "uv"
    assert runtime.agent_root == tmp_path

    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert run_probe(runtime, "import sys") is True
    assert captured["cwd"] == str(tmp_path)


def test_prepare_runtime_python_skips_sync_when_no_pyproject(tmp_path: Path, monkeypatch) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    called: list[object] = []

    def fake_run(*args, **kwargs):
        called.append((args, kwargs))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    runtime = prepare_runtime_python(tmp_path)
    assert runtime.source == "venv"
    assert called == []


def test_prepare_runtime_python_syncs_when_mempalace_missing(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Result:
            returncode = 1 if argv[:1] != ["uv"] else 0

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    prepare_runtime_python(tmp_path)
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)
    assert not any(cmd[:3] == ["uv", "pip", "install"] for cmd in calls)
    assert any("import mempalace" in " ".join(cmd) for cmd in calls)
