"""Tests for knowledge mutation-aware rescan heuristics (hook.py)."""

from __future__ import annotations

from monkeybot.core.knowledge.hook import (
    _argv_from_args,
    command_implies_fs_mutation,
)


def test_readonly_commands() -> None:
    assert not command_implies_fs_mutation(["ls", "."])
    assert not command_implies_fs_mutation(["cat", "README.md"])
    assert not command_implies_fs_mutation(["git", "status"])
    assert not command_implies_fs_mutation(["bash", "-c", "ls -la"])
    assert not command_implies_fs_mutation(["find", ".", "-name", "*.py"])
    assert not command_implies_fs_mutation(["git", "stash", "list"])


def test_compound_shell_commands_trigger_rescan() -> None:
    assert command_implies_fs_mutation(["bash", "-c", "cat foo && rm -rf bar"])
    assert command_implies_fs_mutation(["bash", "-c", "ls; touch new.txt"])
    assert command_implies_fs_mutation(["sh", "-c", "echo hi || mkdir out"])
    assert command_implies_fs_mutation(["bash", "-c", "cat foo | tee out.txt"])


def test_find_delete_and_wrappers() -> None:
    assert command_implies_fs_mutation(["find", ".", "-delete"])
    assert command_implies_fs_mutation(["find", ".", "-exec", "rm", "{}", ";"])
    assert command_implies_fs_mutation(["sudo", "rm", "-rf", "tmp"])
    assert command_implies_fs_mutation(["env", "FOO=1", "rm", "x"])
    assert command_implies_fs_mutation(["git", "stash", "pop"])
    assert command_implies_fs_mutation(["bash", "-c", "cmd 2>/tmp/out.txt"])


def test_unknown_commands_default_to_rescan() -> None:
    assert command_implies_fs_mutation(["mystery-tool"])
    assert command_implies_fs_mutation(["python3", "-c", "print(1)"])
    assert command_implies_fs_mutation(["node", "-e", "1"])


def test_argv_from_args_list_and_json_string() -> None:
    assert _argv_from_args({"argv": ["git", "status"]}) == ["git", "status"]
    assert _argv_from_args({"argv": '["rm", "-rf", "tmp"]'}) == ["rm", "-rf", "tmp"]
    assert _argv_from_args({"argv": '["git", "commit", "-m", "x"]'}) == [
        "git",
        "commit",
        "-m",
        "x",
    ]


def test_argv_from_args_json_string_mutating_triggers_rescan() -> None:
    """Stringified argv must still drive mutation detection after coerce."""
    argv = _argv_from_args({"argv": '["git", "commit", "-m", "msg"]'})
    assert command_implies_fs_mutation(argv)
    argv = _argv_from_args({"argv": '["rm", "-rf", "tmp"]'})
    assert command_implies_fs_mutation(argv)
    argv = _argv_from_args({"argv": '["ls", "."]'})
    assert not command_implies_fs_mutation(argv)


def test_argv_from_args_legacy_and_invalid() -> None:
    assert _argv_from_args({"command": "ls", "args": ["-la"]}) == ["ls", "-la"]
    assert _argv_from_args({"argv": "not-json"}) is None
    assert _argv_from_args({"argv": "not-json", "command": "git", "args": ["status"]}) == [
        "git",
        "status",
    ]
    assert _argv_from_args(None) is None
    assert _argv_from_args({}) is None
