"""Tests for runtime_python resolution."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from monkeybot_cli.runtime_python import (
    CORE_PROBE,
    MANAGED_RUNTIME_SOURCE,
    MEMORY_PROBE,
    RuntimePython,
    RuntimeUpgradeError,
    _managed_memory_requirement,
    _monkeybot_checkout_root,
    gateway_argv,
    managed_memory_runtime_dir,
    mirrored_monkeybot_extras,
    prepare_runtime_python,
    provision_managed_memory_runtime,
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


def _unexpected_provision(*args: object, **kwargs: object) -> RuntimePython:
    raise AssertionError("managed memory runtime should not be provisioned here")


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


def test_prepare_config_only_empty_yaml_reuses_cli_env_when_memory_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-only agent whose CLI env already has MemPalace needs no managed runtime."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "monkeybot_config").mkdir()
    (tmp_path / "monkeybot_config" / "monkeybot.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.provision_managed_memory_runtime",
        _unexpected_provision,
    )

    runtime = prepare_runtime_python(tmp_path)

    assert runtime.source == "cli"


def test_prepare_config_only_memory_disabled_never_provisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_memory_config(tmp_path, enabled=False)
    calls: list[list[str]] = []

    def fake_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
        del timeout
        calls.append([runtime.source, code])
        return True, ""

    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", fake_probe)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.provision_managed_memory_runtime",
        _unexpected_provision,
    )

    runtime = prepare_runtime_python(tmp_path)

    assert runtime.source == "cli"
    assert calls and all("import mempalace" not in code for _source, code in calls)


def test_prepare_config_only_broken_venv_does_not_provision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present ``.venv`` is the agent's interpreter; do not swap in a cache venv."""
    _write_memory_config(tmp_path, enabled=True)
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", lambda *a, **k: (False, "missing"))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.provision_managed_memory_runtime",
        _unexpected_provision,
    )

    with pytest.raises(RuntimeUpgradeError, match="refusing to start"):
        prepare_runtime_python(tmp_path)


def _isolate_managed_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extras: tuple[str, ...] = (),
) -> Path:
    """Point the managed-runtime cache at ``tmp_path`` and pretend uv is installed."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.mirrored_monkeybot_extras",
        lambda: extras,
    )
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._monkeybot_checkout_root",
        lambda: None,
    )
    return managed_memory_runtime_dir(package_version("monkeybot"), extras)


def _fake_uv(calls: list[list[str]], *, install_returncode: int = 0, stderr: str = ""):
    """subprocess.run double that materializes a venv interpreter for ``uv venv``."""

    def fake_run(argv, **kwargs):
        del kwargs
        argv_list = [str(part) for part in argv]
        calls.append(argv_list)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if argv_list[:2] == ["uv", "venv"]:
            interpreter = Path(argv_list[-1]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!/bin/sh\n")
            interpreter.chmod(0o755)
        elif argv_list[:3] == ["uv", "pip", "install"]:
            Result.returncode = install_returncode
            Result.stderr = stderr
        return Result()

    return fake_run


def test_managed_runtime_dir_is_keyed_by_version_and_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    expected = (
        tmp_path
        / "cache"
        / "monkeybot"
        / "runtimes"
        / f"memory-3.1.2-py{sys.version_info.major}.{sys.version_info.minor}"
    )

    assert managed_memory_runtime_dir("3.1.2") == expected
    assert managed_memory_runtime_dir("3.1.2") != managed_memory_runtime_dir("3.1.3")


def test_managed_runtime_reuses_cached_venv_without_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    interpreter = runtime_dir / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)

    def fake_run(argv, **kwargs):
        del kwargs
        raise AssertionError(f"cached runtime must not shell out: {list(argv)}")

    def fake_probe(runtime: RuntimePython, code: str, **kwargs: object) -> bool:
        del code, kwargs
        return runtime.source == MANAGED_RUNTIME_SOURCE

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", fake_probe)

    runtime = provision_managed_memory_runtime(tmp_path)

    assert runtime.source == MANAGED_RUNTIME_SOURCE
    assert runtime.argv == [str(interpreter)]
    assert runtime.agent_root == tmp_path


def test_prepare_config_only_provisions_managed_runtime_when_cache_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "monkeybot_config").mkdir()
    (agent_root / "monkeybot_config" / "monkeybot.yaml").write_text("{}\n", encoding="utf-8")
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    pin = f"monkeybot[memory]=={package_version('monkeybot')}"

    def fake_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
        del timeout
        assert "import mempalace" in code
        return (runtime.source == MANAGED_RUNTIME_SOURCE), ""

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", fake_probe)

    runtime = prepare_runtime_python(agent_root)

    interpreter = runtime_dir / "bin" / "python"
    assert runtime.source == MANAGED_RUNTIME_SOURCE
    assert runtime.argv == [str(interpreter)]
    assert gateway_argv(runtime)[0] == str(interpreter)
    assert calls[0][:5] == ["uv", "venv", "--python", sys.executable, "--relocatable"]
    staging = Path(calls[0][-1])
    assert staging != runtime_dir
    assert calls[1] == [
        "uv",
        "pip",
        "install",
        "--python",
        str(staging / "bin" / "python"),
        pin,
    ]
    assert f"provisioning {pin}" in capsys.readouterr().out


def test_managed_runtime_installs_from_local_checkout_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "monkeybot-src"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "monkeybot"\n', encoding="utf-8")
    _isolate_managed_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._monkeybot_checkout_root",
        lambda: checkout,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda runtime, code, **kwargs: runtime.source == MANAGED_RUNTIME_SOURCE,
    )

    provision_managed_memory_runtime(tmp_path)

    install = next(cmd for cmd in calls if cmd[:3] == ["uv", "pip", "install"])
    requirement = Requirement(install[-1])
    assert requirement.name == "monkeybot"
    assert requirement.extras == {"memory"}
    assert requirement.url == checkout.resolve().as_uri()
    assert str(requirement.specifier) == ""
    assert f"provisioning {install[-1]}" in capsys.readouterr().out


def test_managed_memory_requirement_uses_checkout_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "from-env"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname="monkeybot"\n', encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_CHECKOUT", str(checkout))

    requirement = Requirement(_managed_memory_requirement("3.0.0", "memory"))

    assert requirement.url == checkout.resolve().as_uri()
    assert str(requirement.specifier) == ""
    assert _monkeybot_checkout_root() == checkout.resolve()


def test_managed_runtime_retires_existing_dir_instead_of_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-provision renames the old tree aside so live readers keep their files."""
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    old_interpreter = runtime_dir / "bin" / "python"
    old_interpreter.parent.mkdir(parents=True)
    old_interpreter.write_text("#!/bin/sh\n# stale\n")
    old_interpreter.chmod(0o755)
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        # First probe (cache reuse) fails; staged probe after install passes.
        lambda runtime, code, **kwargs: Path(runtime.argv[0]).resolve() != old_interpreter.resolve(),
    )

    runtime = provision_managed_memory_runtime(tmp_path)

    assert runtime.source == MANAGED_RUNTIME_SOURCE
    assert runtime_dir.is_dir()
    assert (runtime_dir / "bin" / "python").is_file()
    retired = list((runtime_dir.parent).glob(f".{runtime_dir.name}.retired-*"))
    assert len(retired) == 1
    assert (retired[0] / "bin" / "python").read_text() == "#!/bin/sh\n# stale\n"


def test_managed_runtime_pins_the_running_core_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda runtime, code, **kwargs: runtime.source == MANAGED_RUNTIME_SOURCE,
    )

    provision_managed_memory_runtime(tmp_path)

    install = next(cmd for cmd in calls if cmd[:3] == ["uv", "pip", "install"])
    requirement = Requirement(install[-1])
    assert requirement.name == "monkeybot"
    assert requirement.extras == {"memory"}
    assert str(requirement.specifier) == f"=={package_version('monkeybot')}"


def test_managed_runtime_fail_closed_when_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.subprocess.run",
        _fake_uv(calls, install_returncode=1, stderr="no wheel found for onnxruntime"),
    )
    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", lambda *a, **k: False)

    with pytest.raises(RuntimeUpgradeError) as excinfo:
        provision_managed_memory_runtime(tmp_path)

    message = str(excinfo.value)
    assert "MemPalace could not be provisioned" in message
    assert "monkeybot[memory]" in message
    assert "no wheel found for onnxruntime" in message
    assert "memory.enabled: false" in message
    assert "manylinux2014" in message and "musl" in message
    assert not runtime_dir.exists()
    cache_root = tmp_path / "cache" / "monkeybot" / "runtimes"
    assert list(cache_root.glob(".*.tmp-*")) == []


def test_managed_runtime_fail_closed_when_uv_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_managed_cache(tmp_path, monkeypatch)
    monkeypatch.setattr("monkeybot_cli.runtime_python.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.subprocess.run",
        lambda *a, **k: pytest.fail("uv must not be invoked when it is missing"),
    )
    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", lambda *a, **k: False)

    with pytest.raises(RuntimeUpgradeError, match="uv was not found on PATH"):
        provision_managed_memory_runtime(tmp_path)


def test_managed_runtime_fail_closed_when_probe_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", lambda *a, **k: False)

    with pytest.raises(RuntimeUpgradeError, match="MemPalace could not be provisioned"):
        provision_managed_memory_runtime(tmp_path)

    assert any(cmd[:3] == ["uv", "pip", "install"] for cmd in calls)
    assert not runtime_dir.exists()
    cache_root = tmp_path / "cache" / "monkeybot" / "runtimes"
    assert list(cache_root.glob(".*.tmp-*")) == []


def test_managed_runtime_mirrors_installed_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that lives in an extra must survive the interpreter swap."""
    _isolate_managed_cache(tmp_path, monkeypatch, extras=("openai", "postgres"))
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda runtime, code, **kwargs: runtime.source == MANAGED_RUNTIME_SOURCE,
    )

    provision_managed_memory_runtime(tmp_path)

    install = next(cmd for cmd in calls if cmd[:3] == ["uv", "pip", "install"])
    requirement = Requirement(install[-1])
    assert requirement.extras == {"memory", "openai", "postgres"}
    assert str(requirement.specifier) == f"=={package_version('monkeybot')}"


def test_managed_runtime_includes_yaml_provider_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config-only NVIDIA agents need tiktoken even when the CLI env is thin."""
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "model:\n  provider: nvidia\nmemory:\n  enabled: true\n",
        encoding="utf-8",
    )
    _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda runtime, code, **kwargs: runtime.source == MANAGED_RUNTIME_SOURCE,
    )

    provision_managed_memory_runtime(tmp_path)

    install = next(cmd for cmd in calls if cmd[:3] == ["uv", "pip", "install"])
    requirement = Requirement(install[-1])
    assert requirement.extras == {"memory", "nvidia"}


def test_managed_runtime_uses_explicit_config_for_provider_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config`` elsewhere must still drive the provider extra into the pin."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "monkeybot_config").mkdir()
    (agent_root / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  provider: openai\nmemory:\n  enabled: true\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "elsewhere" / "monkeybot.yaml"
    explicit.parent.mkdir()
    explicit.write_text(
        "model:\n  provider: nvidia\nmemory:\n  enabled: true\n",
        encoding="utf-8",
    )
    _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda runtime, code, **kwargs: runtime.source == MANAGED_RUNTIME_SOURCE,
    )

    provision_managed_memory_runtime(agent_root, config_path=explicit)

    install = next(cmd for cmd in calls if cmd[:3] == ["uv", "pip", "install"])
    requirement = Requirement(install[-1])
    assert requirement.extras == {"memory", "nvidia"}


def test_managed_runtime_fail_closed_on_unreadable_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text("model: [\n", encoding="utf-8")
    _isolate_managed_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.subprocess.run",
        lambda *a, **k: pytest.fail("uv must not run when agent YAML is unreadable"),
    )

    with pytest.raises(RuntimeUpgradeError, match="could not read"):
        provision_managed_memory_runtime(tmp_path)


def test_resolve_managed_runtime_tolerates_bad_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Quiet resolve must stay total; bad YAML is fail-closed only at provision."""
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text("model: [\n", encoding="utf-8")
    _isolate_managed_cache(tmp_path, monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="monkeybot_cli.runtime_python"):
        runtime = resolve_runtime_python(tmp_path, memory_enabled=True)

    assert runtime.source == "cli"
    assert runtime.argv == [sys.executable]
    assert "skipping provider extras" in caplog.text


def test_managed_runtime_dir_separates_checkout_from_pypi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkout installs must not reuse a PyPI-keyed cache directory."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    checkout = tmp_path / "src"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "monkeybot"\n', encoding="utf-8")

    pypi = managed_memory_runtime_dir("3.1.2", ("nvidia",))
    from_checkout = managed_memory_runtime_dir("3.1.2", ("nvidia",), checkout=checkout)
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nname = "monkeybot"\n', encoding="utf-8")
    other_checkout = managed_memory_runtime_dir("3.1.2", ("nvidia",), checkout=other)

    assert pypi != from_checkout
    assert from_checkout != other_checkout


def test_managed_runtime_dir_invalidates_on_source_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing packaged source must not silently reuse a snapshot cache dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    checkout = tmp_path / "monkeybot-src"
    pkg = checkout / "src" / "monkeybot"
    pkg.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "monkeybot"\n', encoding="utf-8")
    source = pkg / "providers.py"
    source.write_text("x = 1\n", encoding="utf-8")

    before = managed_memory_runtime_dir("3.1.2", ("nvidia",), checkout=checkout)
    source.write_text("x = 2\n", encoding="utf-8")
    # Force a newer mtime even on filesystems with coarse timestamp resolution.
    st = source.stat()
    os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    after = managed_memory_runtime_dir("3.1.2", ("nvidia",), checkout=checkout)

    assert before != after


def test_managed_runtime_dir_separates_distinct_extra_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the installed extras must not silently reuse a stale runtime."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    bare = managed_memory_runtime_dir("3.1.2")
    openai = managed_memory_runtime_dir("3.1.2", ("openai",))
    both = managed_memory_runtime_dir("3.1.2", ("openai", "postgres"))

    assert len({bare, openai, both}) == 3
    assert managed_memory_runtime_dir("3.1.2", ("postgres", "openai")) == both
    assert managed_memory_runtime_dir("3.1.2", ("OpenAI",)) == openai


def test_mirrored_extras_uses_extra_installed_and_skips_unmirrored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.extra_installed",
        lambda extra: extra in {"openai", "memory", "cli", "postgres", "cli-realtime"},
    )

    assert mirrored_monkeybot_extras() == ("openai", "postgres")


def test_mirrored_extras_skips_base_probe_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``google.genai`` is a core dep — gemini / realtime-gemini must not pollute the mirror."""
    monkeypatch.setattr("monkeybot_cli.runtime_python.extra_installed", lambda extra: True)

    mirrored = mirrored_monkeybot_extras()

    assert "gemini" not in mirrored
    assert "realtime-gemini" not in mirrored
    assert "openai" in mirrored
    assert "postgres" in mirrored


def test_mirrored_extras_is_empty_when_nothing_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("monkeybot_cli.runtime_python.extra_installed", lambda extra: False)

    assert mirrored_monkeybot_extras() == ()


def test_resolve_runtime_python_uses_managed_cache_without_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_memory_config(tmp_path, enabled=True)
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    interpreter = runtime_dir / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda *a, **k: pytest.fail("resolve must not probe the managed cache"),
    )

    runtime = resolve_runtime_python(tmp_path, memory_enabled=True)

    assert runtime.source == MANAGED_RUNTIME_SOURCE
    assert runtime.argv == [str(interpreter)]


def test_prepare_runtime_python_probes_managed_cache_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "monkeybot_config").mkdir()
    (agent_root / "monkeybot_config" / "monkeybot.yaml").write_text("{}\n", encoding="utf-8")
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    interpreter = runtime_dir / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    probes: list[str] = []

    def fake_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
        del timeout
        probes.append(runtime.source)
        assert "import mempalace" in code
        return True, ""

    monkeypatch.setattr("monkeybot_cli.runtime_python._probe", fake_probe)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda *a, **k: pytest.fail("prepare must probe via _probe only"),
    )

    runtime = prepare_runtime_python(agent_root)

    assert runtime.source == MANAGED_RUNTIME_SOURCE
    assert runtime.argv == [str(interpreter)]
    assert probes == [MANAGED_RUNTIME_SOURCE]


def test_resolve_runtime_python_ignores_managed_cache_when_memory_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = _isolate_managed_cache(tmp_path, monkeypatch)
    interpreter = runtime_dir / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.run_probe",
        lambda *a, **k: pytest.fail("memory-off resolve must not probe the cache"),
    )

    runtime = resolve_runtime_python(tmp_path, memory_enabled=False)

    assert runtime.source == "cli"
    assert runtime.argv == [sys.executable]
