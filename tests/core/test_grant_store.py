"""Tests for the cross-agent command/folder grant store."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.tools.grant_store import (
    GrantStoreCache,
    add_command_grant,
    add_path_grant,
    build_grants_persist_hook,
    load_grants,
    remove_command_grant,
    remove_path_grant,
)


def test_load_grants_missing_file_returns_empty(tmp_path: Path) -> None:
    grants = load_grants(tmp_path / "grants.json")
    assert grants.commands == ()
    assert grants.paths == ()


def test_add_and_load_command_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_command_grant(path, command="ffmpeg", created_at="2026-01-01T00:00:00+00:00")
    grants = load_grants(path)
    assert [c.command for c in grants.commands] == ["ffmpeg"]
    # File is machine-owned: mode 0600.
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_add_command_grant_dedupes_by_command(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_command_grant(path, command="ffmpeg", created_at="a")
    add_command_grant(path, command="ffmpeg", created_at="b")
    grants = load_grants(path)
    assert len(grants.commands) == 1
    assert grants.commands[0].created_at == "b"


def test_remove_command_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_command_grant(path, command="ffmpeg", created_at="a")
    assert remove_command_grant(path, command="ffmpeg") is True
    assert load_grants(path).commands == ()
    assert remove_command_grant(path, command="ffmpeg") is False


def test_add_and_load_path_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_path_grant(path, folder="/Users/x/Desktop", mode="read", created_at="a")
    grants = load_grants(path)
    assert grants.paths[0].path == "/Users/x/Desktop"
    assert grants.paths[0].mode == "read"


def test_path_grant_mode_defaults_to_read_on_bad_value(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    path.write_text(
        '{"version": 1, "commands": [], '
        '"paths": [{"path": "/x", "mode": "write", "created_at": "a"}]}',
        encoding="utf-8",
    )
    grants = load_grants(path)
    assert grants.paths[0].mode == "read"


def test_remove_path_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_path_grant(path, folder="/x", mode="read", created_at="a")
    assert remove_path_grant(path, folder="/x") is True
    assert load_grants(path).paths == ()


def test_load_grants_corrupt_json_returns_empty_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    path.write_text("{not json", encoding="utf-8")
    grants = load_grants(path)
    assert grants.commands == ()
    assert grants.paths == ()


def test_commands_and_paths_independent(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_command_grant(path, command="ffmpeg", created_at="a")
    add_path_grant(path, folder="/x", mode="read", created_at="a")
    grants = load_grants(path)
    assert len(grants.commands) == 1
    assert len(grants.paths) == 1
    remove_command_grant(path, command="ffmpeg")
    grants = load_grants(path)
    assert grants.commands == ()
    assert len(grants.paths) == 1


def test_grant_store_cache_reflects_writes_after_mtime_change(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    cache = GrantStoreCache(path)
    assert cache.command_names() == frozenset()
    add_command_grant(path, command="ffmpeg", created_at="a")
    assert cache.command_names() == frozenset({"ffmpeg"})


def test_grant_store_cache_path_grant_for(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    add_path_grant(path, folder="/Users/x/Desktop", mode="read", created_at="a")
    cache = GrantStoreCache(path)
    assert cache.path_grant_for("/Users/x/Desktop") is not None
    assert cache.path_grant_for("/Users/x/Documents") is None


def test_persist_hook_routes_run_command_to_command_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    hook = build_grants_persist_hook(path)
    assert hook("run_command", "ffmpeg") is True
    grants = load_grants(path)
    assert [c.command for c in grants.commands] == ["ffmpeg"]
    assert grants.paths == ()


def test_persist_hook_routes_read_file_to_path_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    hook = build_grants_persist_hook(path)
    for tool in ("read_file", "load_file", "glob", "grep"):
        assert hook(tool, f"/Users/x/{tool}") is True
    grants = load_grants(path)
    assert grants.commands == ()
    assert {p.path for p in grants.paths} == {f"/Users/x/{t}" for t in ("read_file", "load_file", "glob", "grep")}


def test_persist_hook_noop_for_unowned_tool(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    hook = build_grants_persist_hook(path)
    assert hook("write_file", "some/resource") is True
    assert not path.exists()
