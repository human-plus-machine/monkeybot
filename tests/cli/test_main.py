"""Tests for the MonkeyBot realtime talk helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from monkeybot_cli.realtime import talk_ui
from typer.testing import CliRunner

from monkeybot.cli.main import app, run_talk_session

runner = CliRunner()


def test_version_command() -> None:
    from importlib.metadata import version as get_installed_version

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == get_installed_version("monkeybot")


def test_talk_help() -> None:
    result = runner.invoke(app, ["talk", "--help"])
    assert result.exit_code == 0
    assert "talk" in result.output.lower()


def test_talk_text_generates_session_id() -> None:
    # Text mode, so no microphone required; stdin is closed immediately.
    result = runner.invoke(
        app, ["talk", "--text", "--no-start-gateway", "--gateway-url", "ws://localhost:0"]
    )
    # Connection will fail, but the CLI should start and exit.
    assert result.exit_code == 1


def test_talk_auto_start_gateway_fails_without_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-start outside a workspace must say so, not fall through to a connect error.

    Kept hermetic on purpose: this used to read ambient state, so any gateway
    answering on the default port (or a config found by walking up from the repo)
    skipped the branch under test and failed the assertion for unrelated reasons.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    monkeypatch.setattr(talk_ui, "health_ok", lambda base: False)
    result = runner.invoke(app, ["talk", "--text", "--gateway-url", "ws://localhost:0"])
    assert result.exit_code == 1
    assert "Could not find monkeybot_config/monkeybot.yaml" in result.output


def test_talk_spawned_gateway_enables_transcript_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_popen(*args: object, **kwargs: object) -> object:
        captured["env"] = kwargs["env"]
        return object()

    monkeypatch.delenv("MONKEYBOT_TRANSCRIPT_ENABLED", raising=False)
    monkeypatch.setattr(talk_ui.subprocess, "Popen", fake_popen)
    spawned = talk_ui._spawn_combined_gateway(None, tmp_path, 8123)
    try:
        env = captured["env"]
        assert isinstance(env, dict)
        assert env["MONKEYBOT_TRANSCRIPT_ENABLED"] == "1"
    finally:
        spawned.log_file.close()
        spawned.log_path.unlink()


def test_run_talk_session_returns_int_on_connect_failure() -> None:
    code = run_talk_session(
        gateway_url="ws://localhost:0",
        text=True,
        start_gateway=False,
    )
    assert code == 1
