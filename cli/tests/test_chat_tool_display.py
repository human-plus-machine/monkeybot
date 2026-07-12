"""Tests for CLI tool activity display (run_command hints)."""

from __future__ import annotations

import asyncio

import pytest

from monkeybot_cli.chat_tool_display import (
    format_tool_expand_body,
    tool_collapsed_title,
    tool_display,
    tool_hint,
    tool_spinner_prefix,
)
from monkeybot_cli.commands.chat import _TurnActivity


def _run(coro: object) -> None:
    asyncio.run(coro)  # type: ignore[arg-type]


def test_tool_hint_command() -> None:
    assert tool_hint({"command": "git status"}) == "git status"


def test_tool_hint_command_with_args_list() -> None:
    assert tool_hint({"command": "git", "args": ["-C", "repo", "status"]}) == "git -C repo status"


def test_tool_hint_argv() -> None:
    assert tool_hint({"argv": ["python", "-m", "pytest", "tests/"]}) == "python -m pytest tests/"


def test_tool_hint_shell() -> None:
    assert tool_hint({"shell": "ls -la src"}) == "ls -la src"


def test_tool_display_includes_command() -> None:
    assert tool_display("run_command", "run_command", {"command": "echo hi"}) == (
        "run_command — echo hi"
    )


def test_task_tool_display_truncates_long_task_hint() -> None:
    long_task = "Review " + "x" * 80
    args = {"task": long_task}
    display = tool_display("task", "task", args)
    assert display.startswith("subagent — ")
    hint = display.removeprefix("subagent — ")
    assert len(hint) == 61  # 60 chars + ellipsis
    assert hint.endswith("…")
    assert tool_spinner_prefix("task", "task", args).startswith("spawning subagent — ")


def test_task_tool_display_uses_subagent_label() -> None:
    args = {"task": "do the thing"}
    assert tool_display("task", "task", args) == ("subagent — do the thing")
    assert tool_spinner_prefix("task", "task", args) == ("spawning subagent — do the thing")


def test_task_tool_display_includes_subagent_type() -> None:
    args = {"task": "Summarize findings", "subagent_type": "researcher"}
    assert tool_display("task", "task", args) == "subagent:researcher — Summarize findings"
    assert tool_spinner_prefix("task", "task", args) == (
        "spawning subagent:researcher — Summarize findings"
    )


def test_tool_finished_preserves_command_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()

    async def _run_flow() -> None:
        await activity.tool_started("run_command", "run_command", {"command": "git diff"})
        await activity.tool_finished("run_command")

    _run(_run_flow())
    out = capsys.readouterr().out
    assert "run_command — git diff" in out or "git diff" in out


def test_tool_finished_preserves_command_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()

    async def _run_flow() -> None:
        await activity.tool_started("run_command", "run_command", {"argv": ["npm", "test"]})
        await activity.tool_finished("run_command", error="exit code 1")

    _run(_run_flow())
    out = capsys.readouterr().out
    assert "npm test" in out or "run_command" in out


def test_task_tool_finished_shows_subagent_label(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()

    async def _run_flow() -> None:
        await activity.tool_started(
            "task",
            "task",
            {"task": "investigate", "subagent_type": "explore"},
        )
        await activity.tool_finished("task")

    _run(_run_flow())
    out = capsys.readouterr().out
    assert "subagent" in out or "investigate" in out


def test_tool_hint_no_length_limit() -> None:
    long_cmd = "git " + "x" * 200
    assert tool_hint({"command": long_cmd}) == long_cmd


def test_verbose_prints_full_tool_result(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()
    long_result = "line one\n" + ("y" * 300)

    async def _run_flow() -> None:
        await activity.tool_started("run_command", "run_command", {"command": "echo"})
        await activity.tool_finished("run_command", verbose=True, result=long_result)

    _run(_run_flow())
    out = capsys.readouterr().out
    assert long_result in out


def test_verbose_prints_full_subagent_result(capsys: pytest.CaptureFixture[str]) -> None:
    activity = _TurnActivity()
    long_result = "z" * 120

    async def _run_flow() -> None:
        await activity.tool_started("task", "task", {"task": "do work"})
        await activity.tool_finished("task", verbose=True, result=long_result)

    _run(_run_flow())
    out = capsys.readouterr().out
    assert long_result in out


def test_tool_collapsed_title_and_expand_body() -> None:
    assert tool_collapsed_title("run_command", "run_command", {"command": "git status"}) == (
        "Shell  git status"
    )
    assert tool_collapsed_title("read_file", "read_file", {"path": "a.py"}) == "Read  a.py"
    body = format_tool_expand_body(
        "run_command",
        {"command": "echo", "cwd": "/tmp"},
        result="hi",
    )
    assert "**Command**" in body
    assert "**Args**" in body
    assert "`cwd`" in body
    assert "**Result**" in body
