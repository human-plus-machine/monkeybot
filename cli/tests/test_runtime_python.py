"""Tests for runtime_python resolution."""

from __future__ import annotations

import sys
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from monkeybot_cli.runtime_python import (
    MANAGED_RUNTIME_SOURCE,
    RuntimePython,
    RuntimeUpgradeError,
    _monkeybot_extra_requirements,
    gateway_argv,
    managed_memory_runtime_dir,
    mirrored_monkeybot_extras,
    prepare_runtime_python,
    provision_managed_memory_runtime,
    resolve_runtime_python,
    run_probe,
)


def _unexpected_provision(*args: object, **kwargs: object) -> RuntimePython:
    raise AssertionError("managed memory runtime should not be provisioned here")


def _write_memory_config(agent_root: Path, *, enabled: bool) -> None:
    config_dir = agent_root / "monkeybot_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "monkeybot.yaml").write_text(
        f"memory:\n  enabled: {str(enabled).lower()}\n", encoding="utf-8"
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


def test_prepare_config_only_empty_yaml_reuses_cli_env_when_memory_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-only agent whose CLI env already has MemPalace needs no managed runtime."""
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text("{}\n", encoding="utf-8")
    probes: list[str] = []

    def fake_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> bool:
        del timeout
        assert runtime.source == "cli"
        probes.append(code)
        return True

    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", fake_probe)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.provision_managed_memory_runtime",
        _unexpected_provision,
    )

    runtime = prepare_runtime_python(tmp_path)

    assert runtime.source == "cli"
    assert probes and "import mempalace" in probes[0]


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
    """Memory disabled + config-only tree keeps the plain "install monkeybot" failure."""
    from monkeybot_cli.runtime_python import RuntimeUpgradeError

    _write_memory_config(tmp_path, enabled=False)
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", lambda *a, **k: False)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.provision_managed_memory_runtime",
        _unexpected_provision,
    )
    with pytest.raises(RuntimeUpgradeError, match="missing compatible harness packages") as excinfo:
        prepare_runtime_python(tmp_path)
    assert "monkeybot[memory]" not in str(excinfo.value)


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


# --- CLI-managed MemPalace runtime (config-only agents) -------------------------------


def _isolate_managed_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extras: tuple[str, ...] = (),
) -> Path:
    """Point the managed-runtime cache at ``tmp_path`` and pretend uv is installed.

    Mirrored extras are pinned so these tests do not depend on which optional packages
    happen to be installed in the environment running the suite.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.mirrored_monkeybot_extras",
        lambda: extras,
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
            interpreter = Path(argv_list[2]) / "bin" / "python"
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

    def fake_probe(runtime: RuntimePython, code: str, **kwargs: object) -> bool:
        del kwargs
        assert "import mempalace" in code
        return runtime.source == MANAGED_RUNTIME_SOURCE

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", fake_probe)

    runtime = prepare_runtime_python(agent_root)

    interpreter = runtime_dir / "bin" / "python"
    assert runtime.source == MANAGED_RUNTIME_SOURCE
    assert runtime.argv == [str(interpreter)]
    assert gateway_argv(runtime)[0] == str(interpreter)
    assert calls[0] == ["uv", "venv", str(runtime_dir)]
    assert calls[1] == [
        "uv",
        "pip",
        "install",
        "--python",
        str(interpreter),
        f"monkeybot[memory]=={package_version('monkeybot')}",
    ]
    assert "provisioning monkeybot[memory]==" in capsys.readouterr().out


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
    _isolate_managed_cache(tmp_path, monkeypatch)
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
    assert "no wheel found for onnxruntime" in message
    assert "monkeybot-cli[memory]" in message
    assert "memory.enabled: false" in message
    assert "manylinux2014" in message and "musl" in message


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
    _isolate_managed_cache(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", _fake_uv(calls))
    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", lambda *a, **k: False)

    with pytest.raises(RuntimeUpgradeError, match="MemPalace could not be provisioned"):
        provision_managed_memory_runtime(tmp_path)

    assert any(cmd[:3] == ["uv", "pip", "install"] for cmd in calls)


# --- mirroring the CLI env's extras into the managed runtime -------------------------


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
    assert requirement.extras == {"openai", "postgres", "memory"}
    assert str(requirement.specifier) == f"=={package_version('monkeybot')}"


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


def test_mirrored_extras_reports_only_fully_installed_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {
        "openai": [Requirement('openai>=1.0; extra == "openai"')],
        "bedrock": [
            Requirement('anthropic>=0.40.0; extra == "bedrock"'),
            Requirement('boto3>=1.34.0; extra == "bedrock"'),
        ],
        "memory": [Requirement('mempalace>=3.7.0; extra == "memory"')],
        "cli": [Requirement('typer>=0.12.0; extra == "cli"')],
    }
    installed = {"openai", "anthropic", "mempalace", "typer"}
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._monkeybot_extra_requirements",
        lambda: mapping,
    )
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._package_installed",
        lambda name: name in installed,
    )

    # bedrock is partial (no boto3); memory and cli are never mirrored.
    assert mirrored_monkeybot_extras() == ("openai",)


def test_mirrored_extras_resolves_self_referential_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``realtime-gemini = ["monkeybot[realtime]", …]`` must follow the nested extra."""
    mapping = {
        "realtime": [Requirement('websockets>=14.0; extra == "realtime"')],
        "realtime-gemini": [
            Requirement('monkeybot[realtime]; extra == "realtime-gemini"'),
            Requirement('google-genai>=1.0.0; extra == "realtime-gemini"'),
        ],
    }
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._monkeybot_extra_requirements",
        lambda: mapping,
    )
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._package_installed",
        lambda name: name in {"google-genai"},
    )

    # websockets is absent, so the nested monkeybot[realtime] drags both extras down.
    assert mirrored_monkeybot_extras() == ()

    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._package_installed",
        lambda name: name in {"google-genai", "websockets"},
    )
    assert mirrored_monkeybot_extras() == ("realtime", "realtime-gemini")


def test_mirrored_extras_ignores_base_deps_with_unrelated_markers() -> None:
    """Real metadata: base deps carrying markers must not be mistaken for extras."""
    mapping = _monkeybot_extra_requirements()

    assert "memory" in mapping
    assert [req.name for req in mapping["memory"]] == ["mempalace"]
    # httpx is a base dependency of monkeybot and belongs to no extra.
    assert all(req.name != "httpx" for reqs in mapping.values() for req in reqs)


def test_mirrored_extras_is_empty_without_monkeybot_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_not_found() -> dict[str, list[Requirement]]:
        raise PackageNotFoundError("monkeybot")

    monkeypatch.setattr(
        "monkeybot_cli.runtime_python._monkeybot_extra_requirements",
        raise_not_found,
    )

    assert mirrored_monkeybot_extras() == ()


def test_prepare_config_only_memory_disabled_never_provisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_memory_config(tmp_path, enabled=False)
    calls: list[list[str]] = []

    def fake_probe(runtime: RuntimePython, code: str, **kwargs: object) -> bool:
        del kwargs
        calls.append([runtime.source, code])
        return True

    monkeypatch.setattr("monkeybot_cli.runtime_python.run_probe", fake_probe)
    monkeypatch.setattr(
        "monkeybot_cli.runtime_python.provision_managed_memory_runtime",
        _unexpected_provision,
    )

    runtime = prepare_runtime_python(tmp_path)

    assert runtime.source == "cli"
    assert calls and all("import mempalace" not in code for _source, code in calls)
