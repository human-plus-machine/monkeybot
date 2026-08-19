"""Tests for the durable "Always allow" JSON overlay (``computer/approvals.py``)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from monkeybot.computer import approvals as approvals_module
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
    """The exclusive-create lock file is removed once released — unlike an
    flock-held file, which stays on disk (just unlocked) forever. Leaving no
    trace also means a Node process using the identical protocol (see
    `electron/main/agent-approvals.ts::withApprovalsLock`) never has to treat
    an always-present `approvals.json.lock` as ambiguous with a stale hold."""
    add_approval(
        tmp_path / "approvals.json",
        tool="computer_open",
        resource="/x",
        scope="resource",
        created_at="t1",
    )
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"approvals.json"}


class TestFileLock:
    def test_concurrent_add_approval_never_loses_a_write(self, tmp_path: Path) -> None:
        """Two threads racing add_approval for different resources must both
        land — the scenario the lock exists to prevent (a lost read-modify-
        write) would silently drop one of the two."""
        path = tmp_path / "approvals.json"
        errors: list[BaseException] = []

        def add(n: int) -> None:
            try:
                add_approval(
                    path,
                    tool="computer_open",
                    resource=f"/x/{n}",
                    scope="resource",
                    created_at="t",
                )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        records = load_approvals(path)
        assert {r.resource for r in records} == {f"/x/{n}" for n in range(8)}

    def test_stale_lock_is_reclaimed_not_waited_out(self, tmp_path: Path) -> None:
        """A lock file older than the staleness threshold is from a crashed
        holder (real holds are a few ms), not a live one — it must be
        reclaimed rather than blocking the timeout out."""
        path = tmp_path / "approvals.json"
        lock_path = approvals_module._lock_path(path)  # noqa: SLF001
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999999")
        stale_mtime = time.time() - (approvals_module._LOCK_STALE_S + 1)  # noqa: SLF001
        os.utime(lock_path, (stale_mtime, stale_mtime))

        start = time.monotonic()
        add_approval(path, tool="computer_open", resource="/x", scope="resource", created_at="t")
        elapsed = time.monotonic() - start

        assert elapsed < approvals_module._LOCK_TIMEOUT_S  # noqa: SLF001
        assert load_approvals(path)[0].resource == "/x"

    def test_lock_file_removed_when_holder_raises(self, tmp_path: Path) -> None:
        """A failure inside the critical section must not leave the lock held
        forever — the next caller would otherwise wait out the full timeout
        and then wrongly treat a fresh lock as stale."""
        path = tmp_path / "approvals.json"
        with pytest.raises(RuntimeError), approvals_module._file_lock(path):  # noqa: SLF001
            raise RuntimeError("boom")
        assert not approvals_module._lock_path(path).exists()  # noqa: SLF001


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
