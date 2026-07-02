"""Tests for run_command argv parsing."""

from __future__ import annotations

from monkeybot.core.tools.core_tool_executor import _parse_run_command


def test_parse_run_command_argv_list() -> None:
    cmd, argv = _parse_run_command({"argv": ["ls", "-R", "."]})
    assert cmd == "ls"
    assert argv == ["-R", "."]


def test_parse_run_command_command_with_args() -> None:
    cmd, argv = _parse_run_command({"command": "ls", "args": ["-la"]})
    assert cmd == "ls"
    assert argv == ["-la"]


def test_parse_run_command_splits_compound_command_when_args_empty() -> None:
    cmd, argv = _parse_run_command({"command": "ls -R", "args": []})
    assert cmd == "ls"
    assert argv == ["-R"]


def test_parse_run_command_bare_binary_empty_args() -> None:
    cmd, argv = _parse_run_command({"command": "ls", "args": []})
    assert cmd == "ls"
    assert argv == []


def test_parse_run_command_shell_script() -> None:
    cmd, argv = _parse_run_command({"shell": "grep -n foo bar.txt"})
    assert cmd == "grep"
    assert argv == ["-n", "foo", "bar.txt"]


def test_parse_run_command_wraps_and_chain_with_bash_c() -> None:
    cmd, argv = _parse_run_command({"command": 'echo "sandbox works" && date', "args": []})
    assert cmd == "bash"
    assert argv == ["-c", 'echo "sandbox works" && date']


def test_parse_run_command_wraps_semicolon_chain() -> None:
    cmd, argv = _parse_run_command({"command": "echo a; echo b", "args": []})
    assert cmd == "bash"
    assert argv == ["-c", "echo a; echo b"]


def test_parse_run_command_wraps_pipe() -> None:
    cmd, argv = _parse_run_command({"command": "echo hi | wc -c", "args": []})
    assert cmd == "bash"
    assert argv == ["-c", "echo hi | wc -c"]


def test_parse_run_command_does_not_wrap_bash_c_invocation() -> None:
    cmd, argv = _parse_run_command({"command": 'bash -c "echo ok"', "args": []})
    assert cmd == "bash"
    assert argv == ["-c", "echo ok"]
