"""Tests for the durable "Always allow" JSON overlay (``computer/approvals.py``)."""

from __future__ import annotations

import json
from pathlib import Path

from monkeybot.computer.approvals import (
    add_approval,
    load_approvals,
    remove_approval,
    to_permission_rules,
)


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_approvals(tmp_path / "approvals.json") == []


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    path.write_text("{not valid json")
    assert load_approvals(path) == []


def test_add_then_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "monkeybot_config" / "approvals.json"
    add_approval(
        path, tool="computer_open", resource="/Users/x/Downloads", scope="resource", created_at="t1"
    )
    records = load_approvals(path)
    assert len(records) == 1
    assert records[0].tool == "computer_open"
    assert records[0].resource == "/Users/x/Downloads"
    assert records[0].scope == "resource"


def test_add_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "approvals.json"
    add_approval(path, tool="computer_clipboard_read", resource="*", scope="tool", created_at="t1")
    assert path.exists()


def test_add_is_idempotent_for_same_tool_resource(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    add_approval(path, tool="computer_open", resource="/x", scope="resource", created_at="t1")
    add_approval(path, tool="computer_open", resource="/x", scope="resource", created_at="t2")
    records = load_approvals(path)
    assert len(records) == 1
    assert records[0].created_at == "t2"


def test_add_two_different_resources(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    add_approval(path, tool="computer_open", resource="/x", scope="resource", created_at="t1")
    add_approval(path, tool="computer_open", resource="/y", scope="resource", created_at="t1")
    records = load_approvals(path)
    assert {r.resource for r in records} == {"/x", "/y"}


def test_remove_existing(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    add_approval(path, tool="computer_open", resource="/x", scope="resource", created_at="t1")
    assert remove_approval(path, tool="computer_open", resource="/x") is True
    assert load_approvals(path) == []


def test_remove_missing_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    assert remove_approval(path, tool="computer_open", resource="/x") is False


def test_written_file_is_valid_pretty_json(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    add_approval(path, tool="computer_open", resource="/x", scope="resource", created_at="t1")
    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert isinstance(payload["approvals"], list)


def test_no_stray_temp_files_left_behind(tmp_path: Path) -> None:
    add_approval(
        tmp_path / "approvals.json",
        tool="computer_open",
        resource="/x",
        scope="resource",
        created_at="t1",
    )
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"approvals.json", "approvals.json.lock"}


class TestToPermissionRules:
    def test_resource_scope_escapes_glob_metacharacters(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.json"
        add_approval(
            path,
            tool="computer_open",
            resource="/Users/x/Screen Shot [1].png",
            scope="resource",
            created_at="t1",
        )
        records = load_approvals(path)
        triples = to_permission_rules(records)
        assert len(triples) == 1
        tool, pattern, effect = triples[0]
        assert tool == "computer_open"
        assert effect == "allow"
        # The escaped pattern must match the literal string, not any other file.
        import fnmatch

        assert fnmatch.fnmatchcase("/Users/x/Screen Shot [1].png", pattern)
        assert not fnmatch.fnmatchcase("/Users/x/Screen Shot X.png", pattern)

    def test_tool_scope_uses_wildcard_pattern(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.json"
        add_approval(
            path, tool="computer_clipboard_read", resource="*", scope="tool", created_at="t1"
        )
        records = load_approvals(path)
        triples = to_permission_rules(records)
        assert triples == [("computer_clipboard_read", "*", "allow")]
