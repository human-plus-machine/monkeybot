"""Tests for runtime_python resolution."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

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
    assert any("-c" in args[0] for args, _kwargs in called)
    assert not any(args[0][:1] == ["uv"] for args, _kwargs in called)


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
            returncode = 0

        argv_list = list(argv)
        if argv_list[:1] != ["uv"] and "-c" in argv_list:
            # First harness probe fails; the post-upgrade probe succeeds.
            Result.returncode = 1 if not any(cmd[:2] == ["uv", "sync"] for cmd in calls[:-1]) else 0
        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    prepare_runtime_python(tmp_path)
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)
    assert any(cmd[:3] == ["uv", "lock", "--upgrade-package"] for cmd in calls)
    assert not any(cmd[:3] == ["uv", "pip", "install"] for cmd in calls)
    assert any("import mempalace" in " ".join(cmd) for cmd in calls)


def test_prepare_runtime_python_removes_memory_extra_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "monkeybot_config").mkdir()
    (tmp_path / "monkeybot_config" / "monkeybot.yaml").write_text(
        "memory:\n  enabled: false\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        (
            "[project]\n"
            'name = "agent"\n'
            'version = "0.1.0"\n'
            'dependencies = ["monkeybot[memory]>=3.0.0,<4"]\n'
        ),
        encoding="utf-8",
    )
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
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)

    prepare_runtime_python(tmp_path)

    document = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
    refreshed = Requirement(document["project"]["dependencies"][0])
    assert not refreshed.extras
    assert str(refreshed.specifier) == str(Requirement("monkeybot>=3.0.0,<4").specifier)
    probe_commands = [cmd for cmd in calls if "-c" in cmd]
    assert probe_commands
    assert all("import mempalace" not in " ".join(cmd) for cmd in probe_commands)
    assert any(cmd[:3] == ["uv", "lock", "--upgrade-package"] for cmd in calls)
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)


@pytest.mark.parametrize(
    ("default_enabled", "explicit_enabled", "expects_memory"),
    [(False, True, True), (True, False, False)],
)
def test_prepare_runtime_python_uses_explicit_config_for_memory_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default_enabled: bool,
    explicit_enabled: bool,
    expects_memory: bool,
) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text(
        f"memory:\n  enabled: {str(default_enabled).lower()}\n",
        encoding="utf-8",
    )
    explicit_config = tmp_path / "alternate.yaml"
    explicit_config.write_text(
        f"memory:\n  enabled: {str(explicit_enabled).lower()}\n",
        encoding="utf-8",
    )
    initial = "monkeybot>=3.0.0,<4"
    if not expects_memory:
        initial = "monkeybot[memory]>=3.0.0,<4"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "agent"\ndependencies = ["{initial}"]\n',
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)

    prepare_runtime_python(tmp_path, explicit_config)

    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert ("monkeybot[memory]" in text) is expects_memory
    probes = [" ".join(cmd) for cmd in calls if "-c" in cmd]
    assert probes
    assert any("import mempalace" in command for command in probes) is expects_memory


def test_prepare_runtime_python_refreshes_stale_pyproject_before_lock(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        (
            "[project]\n"
            'name = "agent"\n'
            "dependencies = [\n"
            '  "monkeybot[nvidia,sandbox,web-search,observability]>=2.1.0,<3",\n'
            "]\n"
        ),
        encoding="utf-8",
    )
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    pyproject_at_lock: list[str] = []
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        del kwargs
        argv_list = list(argv)
        calls.append(argv_list)
        if argv_list[:3] == ["uv", "lock", "--upgrade-package"]:
            pyproject_at_lock.append((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))

        class Result:
            returncode = 0

        if argv_list[:1] != ["uv"] and "-c" in argv_list:
            Result.returncode = 1 if not any(cmd[:2] == ["uv", "sync"] for cmd in calls[:-1]) else 0
        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    prepare_runtime_python(tmp_path)
    assert pyproject_at_lock
    assert "memory" in pyproject_at_lock[0]
    assert ">=3.0.0,<4" in pyproject_at_lock[0]


def test_prepare_runtime_python_fail_closed_when_upgrade_stale(tmp_path: Path, monkeypatch) -> None:
    from monkeybot_cli.runtime_python import RuntimeUpgradeError

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    def fake_run(argv, **kwargs):
        del argv, kwargs

        class Result:
            returncode = 1
            stderr = "No module named 'mempalace'"
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    with pytest.raises(RuntimeUpgradeError, match="refusing to start"):
        prepare_runtime_python(tmp_path)


def test_prepare_runtime_python_fail_closed_without_pyproject(tmp_path: Path, monkeypatch) -> None:
    from monkeybot_cli.runtime_python import RuntimeUpgradeError

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", lambda *a, **k: False)
    with pytest.raises(RuntimeUpgradeError, match="missing compatible harness packages"):
        prepare_runtime_python(tmp_path)


def test_prepare_runtime_python_probes_uv_runtime(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Result:
            returncode = 0

        argv_list = list(argv)
        if argv_list[:2] == ["uv", "run"] and "-c" in argv_list:
            probes["n"] += 1
            Result.returncode = 0 if probes["n"] > 1 else 1
        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    runtime = prepare_runtime_python(tmp_path)
    assert runtime.source == "uv"
    assert any(cmd[:3] == ["uv", "lock", "--upgrade-package"] for cmd in calls)
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)
