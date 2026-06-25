"""Tests for CLI tool activity display (run_command hints)."""

from __future__ import annotations

import asyncio

import pytest

from monkeybot_cli.commands.chat import _TurnActivity, _tool_display, _tool_hint


def test_tool_hint_command() -> None:
    assert _tool_hint({"command": "git status"}) == "git status"


def test_tool_hint_command_with_args_list() -> None:
    assert _tool_hint({"command": "git", "args": ["-C", "repo", "status"]}) == "git -C repo status"


def test_tool_hint_argv() -> None:
    assert _tool_hint({"argv": ["python", "-m", "pytest", "tests/"]}) == "python -m pytest tests/"


def test_tool_hint_shell() -> None:
    assert _tool_hint({"shell": "ls -la src"}) == "ls -la src"


def test_tool_display_includes_command() -> None:
    assert _tool_display("run_command", "run_command", {"command": "echo hi"}) == (
        "run_command — echo hi"
    )


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_tool_finished_preserves_command_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()

    async def _run_flow() -> None:
        await activity.tool_started("run_command", "run_command", {"command": "git diff"})
        await activity.tool_finished("run_command")

    _run(_run_flow())
    out = capsys.readouterr().out
    assert "run_command — git diff" in out
    assert "✓" in out or "\x1b[32m✓" in out


def test_tool_finished_preserves_command_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()

    async def _run_flow() -> None:
        await activity.tool_started("run_command", "run_command", {"argv": ["npm", "test"]})
        await activity.tool_finished("run_command", error="exit code 1")

    _run(_run_flow())
    out = capsys.readouterr().out
    assert "run_command — npm test" in out
    assert "exit code 1" in out
