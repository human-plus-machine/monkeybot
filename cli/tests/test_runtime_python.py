"""Tests for runtime_python resolution."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from monkeybot_cli.runtime_python import (
    CORE_PROBE,
    MEMORY_PROBE,
    RuntimePython,
    RuntimeUpgradeError,
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


def test_run_probe_returns_false_when_interpreter_missing(monkeypatch) -> None:
    runtime = RuntimePython(["uv", "run", "python"], "uv", Path("/missing-agent"))

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("uv")

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert run_probe(runtime, "import sys") is False


def test_run_probe_returns_false_on_timeout(monkeypatch) -> None:
    runtime = RuntimePython([sys.executable], "cli")

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="python", timeout=15)

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert run_probe(runtime, "import sys") is False


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
            stderr = ""
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert run_probe(runtime, "import sys") is True
    assert captured["cwd"] == str(tmp_path)


def test_core_probe_matches_compatible_range() -> None:
    from packaging.specifiers import SpecifierSet

    from monkeybot_cli.compat import (
        COMPATIBLE_CORE_LOWER_VERSION,
        COMPATIBLE_CORE_RANGE,
        COMPATIBLE_CORE_UPPER_VERSION,
        _bounds_from_range,
    )

    spec = SpecifierSet(COMPATIBLE_CORE_RANGE)
    assert COMPATIBLE_CORE_RANGE == ">=3.0.0,<4"
    assert _bounds_from_range(COMPATIBLE_CORE_RANGE) == (
        COMPATIBLE_CORE_LOWER_VERSION,
        COMPATIBLE_CORE_UPPER_VERSION,
    )
    assert COMPATIBLE_CORE_LOWER_VERSION == "3.0.0"
    assert COMPATIBLE_CORE_UPPER_VERSION == "4"
    assert "packaging" not in CORE_PROBE
    assert "assert " not in CORE_PROBE
    assert repr(COMPATIBLE_CORE_LOWER_VERSION) in CORE_PROBE
    assert repr(COMPATIBLE_CORE_UPPER_VERSION) in CORE_PROBE
    assert MEMORY_PROBE.startswith("import mempalace")

    def _probe_version(ver: str, *, optimize: bool = False) -> int:
        snippet = CORE_PROBE.replace('version("monkeybot")', repr(ver), 1)
        cmd = [sys.executable, "-O", "-c", snippet] if optimize else [sys.executable, "-c", snippet]
        return subprocess.run(cmd, capture_output=True, text=True).returncode

    cases = (
        "3.0.0",
        "3.9.9",
        "3.1.0+local",
        "3.1.post1",
        "3.1.post",
        "v3.1",
        " 3.1 ",
        "3.0rc1",
        "3.1rc1",
        "1!3.1.0",
        "2.9.9",
        "4.0.0",
        "3.1.dev0",
        "3.0.0a1",
    )
    for ver in cases:
        assert (_probe_version(ver) == 0) is spec.contains(ver), ver
    assert _probe_version("2.9.9", optimize=True) != 0
    assert _probe_version("3.1.0", optimize=True) == 0


def _write_memory_config(root: Path, *, enabled: bool) -> None:
    cfg = root / "monkeybot_config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "monkeybot.yaml").write_text(
        f"memory:\n  enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )


def test_prepare_runtime_python_project_memory_remediation_includes_extra(
    tmp_path: Path, monkeypatch
) -> None:
    _write_memory_config(tmp_path, enabled=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", lambda *a, **k: (False, "missing"))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 1})(),
    )
    with pytest.raises(RuntimeUpgradeError, match="monkeybot\\[memory\\]") as excinfo:
        prepare_runtime_python(tmp_path)
    assert "pyproject.toml" in str(excinfo.value)


def test_prepare_runtime_python_skips_sync_when_probe_passes(tmp_path: Path, monkeypatch) -> None:
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
            stderr = ""
            stdout = ""

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
    original = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        argv_list = list(argv)
        if argv_list[:1] != ["uv"] and "-c" in argv_list:
            Result.returncode = 1 if not any(cmd[:2] == ["uv", "sync"] for cmd in calls[:-1]) else 0
        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    prepare_runtime_python(tmp_path)
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)
    assert not any(cmd[:3] == ["uv", "lock", "--upgrade-package"] for cmd in calls)
    assert not any(cmd[:3] == ["uv", "pip", "install"] for cmd in calls)
    assert any("import mempalace" in " ".join(cmd) for cmd in calls)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original


def test_prepare_runtime_python_probes_core_only_when_memory_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    _write_memory_config(tmp_path, enabled=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    probes: list[str] = []

    def fake_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
        del runtime, timeout
        probes.append(code)
        return True, ""

    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", fake_probe)
    prepare_runtime_python(tmp_path)
    assert probes
    assert all("import mempalace" not in code for code in probes)


def test_prepare_runtime_python_uses_explicit_config_for_memory_probe(
    tmp_path: Path, monkeypatch
) -> None:
    _write_memory_config(tmp_path, enabled=True)
    explicit = tmp_path / "alternate.yaml"
    explicit.write_text("memory:\n  enabled: false\n", encoding="utf-8")
    probes: list[str] = []

    def fake_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
        del runtime, timeout
        probes.append(code)
        return True, ""

    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", fake_probe)
    prepare_runtime_python(tmp_path, explicit)
    assert probes
    assert all("import mempalace" not in code for code in probes)


def test_prepare_runtime_python_fail_closed_when_sync_stale(tmp_path: Path, monkeypatch) -> None:
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
            returncode = 1
            stderr = "No module named 'mempalace'"
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    with pytest.raises(RuntimeUpgradeError, match="refusing to start"):
        prepare_runtime_python(tmp_path)
    assert (tmp_path / "pyproject.toml").read_text(
        encoding="utf-8"
    ) == '[project]\nname = "agent"\n'
    assert sum(1 for cmd in calls if "-c" in cmd) == 2
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)


def test_prepare_runtime_python_fail_closed_when_uv_missing(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    def fake_run(argv, **kwargs):
        del kwargs
        if argv[:2] == ["uv", "sync"]:
            raise FileNotFoundError("uv")

        class Result:
            returncode = 1
            stderr = ""
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    with pytest.raises(RuntimeUpgradeError, match="refusing to start"):
        prepare_runtime_python(tmp_path)


def test_prepare_runtime_python_fail_closed_without_pyproject(tmp_path: Path, monkeypatch) -> None:
    _write_memory_config(tmp_path, enabled=False)
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", lambda *a, **k: (False, "missing"))
    with pytest.raises(RuntimeUpgradeError, match="missing a compatible MonkeyBot") as excinfo:
        prepare_runtime_python(tmp_path)
    assert "uv sync" not in str(excinfo.value)
    assert "memory.enabled" not in str(excinfo.value)


def test_prepare_runtime_python_fail_closed_config_only_with_memory(
    tmp_path: Path, monkeypatch
) -> None:
    _write_memory_config(tmp_path, enabled=True)
    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", lambda *a, **k: (False, "missing"))
    with pytest.raises(RuntimeUpgradeError, match="MemPalace") as excinfo:
        prepare_runtime_python(tmp_path)
    assert "memory.enabled: false" in str(excinfo.value)
    assert "monkeybot[memory]" in str(excinfo.value)
    assert "uv sync" not in str(excinfo.value)


def test_prepare_runtime_python_probes_uv_runtime(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        argv_list = list(argv)
        if argv_list[:2] == ["uv", "run"] and "-c" in argv_list:
            probes["n"] += 1
            Result.returncode = 0 if probes["n"] > 1 else 1
        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    runtime = prepare_runtime_python(tmp_path)
    assert runtime.source == "uv"
    assert any(cmd[:2] == ["uv", "sync"] for cmd in calls)
    assert not any(cmd[:3] == ["uv", "lock", "--upgrade-package"] for cmd in calls)
    assert sum(1 for cmd in calls if "-c" in cmd) == 2


def test_prepare_runtime_python_fail_closed_when_initial_uv_missing(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent"\n', encoding="utf-8")

    def fake_run(argv, **kwargs):
        del kwargs
        raise FileNotFoundError(argv[0] if argv else "uv")

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    with pytest.raises(RuntimeUpgradeError, match="refusing to start"):
        prepare_runtime_python(tmp_path)


def test_prepare_runtime_python_probes_once_without_pyproject(tmp_path: Path, monkeypatch) -> None:
    _write_memory_config(tmp_path, enabled=False)
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
            returncode = 1
            stderr = "No module named 'monkeybot'"
            stdout = ""

        return Result()

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    with pytest.raises(RuntimeUpgradeError, match="refusing to start"):
        prepare_runtime_python(tmp_path)
    assert sum(1 for cmd in calls if "-c" in cmd) == 1
    assert not any(cmd[:2] == ["uv", "sync"] for cmd in calls)
