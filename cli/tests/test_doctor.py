"""Tests for doctor remediation copy."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from monkeybot_cli.commands.doctor import (
    _add_ollama_local_checks,
    _agent_defines_project_extra,
    _extra_remediation,
    _is_local_ollama_provider,
    _runtime_python_version,
)
from monkeybot_cli.output import CommandReport
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
    assert "refresh the managed runtime" not in text


def test_extra_remediation_config_only_managed_runtime(tmp_path: Path) -> None:
    runtime = SimpleNamespace(source="cli-managed")
    text = _extra_remediation("openai", tmp_path, runtime)
    assert "uv tool install --with 'monkeybot[openai]' monkeybot-cli" in text
    assert "refresh the managed runtime" in text


def test_agent_defines_project_extra(tmp_path: Path) -> None:
    assert not _agent_defines_project_extra(tmp_path, "openai")
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\npostgres = []\n",
        encoding="utf-8",
    )
    assert _agent_defines_project_extra(tmp_path, "postgres")
    assert not _agent_defines_project_extra(tmp_path, "openai")


def test_runtime_python_version_returns_zeros_when_uv_missing(tmp_path: Path, monkeypatch) -> None:
    runtime = RuntimePython(["uv", "run", "python"], "uv", tmp_path)

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("uv")

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert _runtime_python_version(runtime) == (0, 0, 0)


def test_runtime_python_version_returns_zeros_on_timeout(tmp_path: Path, monkeypatch) -> None:
    runtime = RuntimePython(["uv", "run", "python"], "uv", tmp_path)

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="uv", timeout=15)

    monkeypatch.setattr("monkeybot_cli.runtime_python.subprocess.run", fake_run)
    assert _runtime_python_version(runtime) == (0, 0, 0)


def test_is_local_ollama_provider() -> None:
    assert _is_local_ollama_provider("ollama-local")
    assert _is_local_ollama_provider("ollama_local")
    assert _is_local_ollama_provider("ollama")
    assert not _is_local_ollama_provider("ollama-cloud")
    assert not _is_local_ollama_provider("gemini")


def _doctor_by_id(report: CommandReport) -> dict[str, object]:
    return {c.id: c for c in report.checks}


def test_ollama_local_checks_skip_for_cloud() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-cloud",
        model_name="qwen3:8b-mlx",
        thinking_budget=-1,
        num_ctx=100_000,
    )
    by_id = _doctor_by_id(report)
    assert by_id["ollama.local.mlx_runner"].status == "skip"
    assert by_id["ollama.local.thinking_default"].status == "skip"
    assert by_id["ollama.local.num_ctx_invalid"].status == "skip"
    assert by_id["ollama.local.num_ctx_large"].status == "skip"


def test_ollama_local_checks_warn_mlx_thinking_and_large_ctx() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="qwen3:8b-mlx",
        thinking_budget=-1,
        num_ctx=65_536,
    )
    by_id = _doctor_by_id(report)
    assert by_id["ollama.local.mlx_runner"].status == "fail"
    assert by_id["ollama.local.mlx_runner"].severity == "warning"
    assert "docs/ollama-local.md" in (by_id["ollama.local.mlx_runner"].remediation or "")
    assert by_id["ollama.local.thinking_default"].status == "fail"
    assert by_id["ollama.local.num_ctx_invalid"].status == "pass"
    assert by_id["ollama.local.num_ctx_large"].status == "fail"


def test_ollama_local_checks_pass_when_tuned() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="llama3.1:8b",
        thinking_budget=0,
        num_ctx=8192,
    )
    by_id = _doctor_by_id(report)
    assert by_id["ollama.local.mlx_runner"].status == "pass"
    assert by_id["ollama.local.thinking_default"].status == "skip"
    assert by_id["ollama.local.num_ctx_invalid"].status == "pass"
    assert by_id["ollama.local.num_ctx_large"].status == "pass"


def test_ollama_local_num_ctx_unset_is_skipped() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="llama3.1:8b",
        thinking_budget=0,
        num_ctx=None,
    )
    by_id = _doctor_by_id(report)
    assert by_id["ollama.local.num_ctx_invalid"].status == "skip"
    assert by_id["ollama.local.num_ctx_large"].status == "skip"


def test_ollama_local_num_ctx_garbage_fails() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="llama3.1:8b",
        thinking_budget=0,
        num_ctx="abc",
    )
    by_id = _doctor_by_id(report)
    assert by_id["ollama.local.num_ctx_invalid"].status == "fail"
    assert by_id["ollama.local.num_ctx_invalid"].severity == "error"
    assert by_id["ollama.local.num_ctx_large"].status == "skip"


def test_ollama_local_num_ctx_zero_fails() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="llama3.1:8b",
        thinking_budget=0,
        num_ctx=0,
    )
    by_id = _doctor_by_id(report)
    assert by_id["ollama.local.num_ctx_invalid"].status == "fail"
    assert by_id["ollama.local.num_ctx_large"].status == "skip"


def test_ollama_local_thinking_default_skips_non_reasoning() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="llama3.1:8b",
        thinking_budget=-1,
        num_ctx=None,
    )
    assert _doctor_by_id(report)["ollama.local.thinking_default"].status == "skip"


def test_ollama_local_thinking_default_pass_when_off() -> None:
    report = CommandReport(command="doctor", ok=True, config_path=None)
    _add_ollama_local_checks(
        report,
        provider="ollama-local",
        model_name="qwen3:8b",
        thinking_budget=0,
        num_ctx=8192,
    )
    assert _doctor_by_id(report)["ollama.local.thinking_default"].status == "pass"
