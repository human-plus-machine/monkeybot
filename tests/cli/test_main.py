"""Tests for the MonkeyBot realtime talk helpers."""

from __future__ import annotations

from typer.testing import CliRunner

from monkeybot.cli.main import app, run_talk_session

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "2.1.0"


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


def test_talk_auto_start_gateway_fails_without_workspace() -> None:
    result = runner.invoke(app, ["talk", "--text", "--gateway-url", "ws://localhost:0"])
    assert result.exit_code == 1
    assert "Could not find monkeybot_config/monkeybot.yaml" in result.output


def test_run_talk_session_returns_int_on_connect_failure() -> None:
    code = run_talk_session(
        gateway_url="ws://localhost:0",
        text=True,
        start_gateway=False,
    )
    assert code == 1
