"""Tests for run_command argv parsing."""

from __future__ import annotations

import json

import pytest

from monkeybot.core.tools.core_tool_executor import _parse_run_command
from monkeybot.core.tools.inspector import coerce_run_command_argv


def test_parse_run_command_argv_list() -> None:
    cmd, argv = _parse_run_command({"argv": ["ls", "-R", "."]})
    assert cmd == "ls"
    assert argv == ["-R", "."]


def test_parse_run_command_argv_json_string() -> None:
    """LLM quirk: argv sent as a JSON-encoded list string instead of a real array."""
    raw = json.dumps(["git", "-C", "repos/foo", "grep", "-n", "retry", "file.py"])
    cmd, argv = _parse_run_command({"argv": raw})
    assert cmd == "git"
    assert argv == ["-C", "repos/foo", "grep", "-n", "retry", "file.py"]


def test_parse_run_command_argv_invalid_string_raises() -> None:
    with pytest.raises(ValueError, match="argv must be an array"):
        _parse_run_command({"argv": "git status"})


def test_coerce_run_command_argv_list_and_json_string() -> None:
    assert coerce_run_command_argv(["ls", "."]) == ["ls", "."]
    assert coerce_run_command_argv('["grep", "-n", "foo", "a.py"]') == [
        "grep",
        "-n",
        "foo",
        "a.py",
    ]
    assert coerce_run_command_argv(None) is None
    assert coerce_run_command_argv([]) is None


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
