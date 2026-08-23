"""Tests for session-scoped spill cleanup and inventory previews."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.tools.spill_inventory import (
    _build_spill_preview,
    cleanup_session_spill_files,
    session_spill_dirs,
    spill_inventory_note,
)


def test_session_spill_dirs_includes_parent_and_subagent(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    (root / "sess-1").mkdir(parents=True)
    (root / "subagent:sess-1:aaa").mkdir(parents=True)
    (root / "subagent:sess-1:bbb").mkdir(parents=True)
    (root / "sess-2").mkdir(parents=True)
    (root / "subagent:sess-2:ccc").mkdir(parents=True)

    dirs = session_spill_dirs(tmp_path, "sess-1")
    names = {p.name for p in dirs}
    assert names == {"sess-1", "subagent:sess-1:aaa", "subagent:sess-1:bbb"}


def test_session_spill_dirs_glob_metachar_does_not_match_other_sessions(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    (root / "_").mkdir(parents=True)
    (root / "subagent:_:aaa").mkdir(parents=True)
    (root / "subagent:sess-1:bbb").mkdir(parents=True)

    dirs = session_spill_dirs(tmp_path, "*")
    names = {p.name for p in dirs}
    assert names == {"_", "subagent:_:aaa"}
    assert "subagent:sess-1:bbb" not in names


def test_session_spill_dirs_legacy_glob_metachar_id(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    legacy_name = "sess*1"
    (root / legacy_name).mkdir(parents=True)
    (root / legacy_name / "old.txt").write_text("legacy", encoding="utf-8")

    dirs = session_spill_dirs(tmp_path, legacy_name)
    names = {p.name for p in dirs}
    assert legacy_name in names
    assert "sess_1" in names


def test_cleanup_session_spill_skips_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    victim = root / "victim"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("stay", encoding="utf-8")
    link = root / "s1"
    link.symlink_to(victim, target_is_directory=True)

    import asyncio

    asyncio.run(cleanup_session_spill_files(tmp_path, "s1"))

    assert not link.exists()
    assert victim.exists()
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "stay"


@pytest.mark.asyncio
async def test_cleanup_session_spill_files_concurrent(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    for name in ("s1", "subagent:s1:one", "subagent:s1:two", "other"):
        d = root / name
        d.mkdir(parents=True)
        (d / "x.txt").write_text("data", encoding="utf-8")

    await cleanup_session_spill_files(tmp_path, "s1")

    assert not (root / "s1").exists()
    assert not (root / "subagent:s1:one").exists()
    assert not (root / "subagent:s1:two").exists()
    assert (root / "other" / "x.txt").read_text(encoding="utf-8") == "data"


def test_spill_preview_unwraps_run_command_json_stdout() -> None:
    rows = [{"id": i, "title": f"pr-{i}", "state": "OPEN"} for i in range(40)]
    payload = json.dumps({"ok": True, "stdout": json.dumps(rows)})
    kind, preview, unwrapped, _body_lines = _build_spill_preview(
        payload, tool_name="run_command"
    )
    assert kind == "json"
    assert unwrapped is True
    assert "pr-0" in preview
    assert len(preview) < len(payload)
    assert preview.count('"id"') <= 20


def test_spill_inventory_note_includes_preview_not_full_body() -> None:
    body = ("line-%s\n" % ("x" * 200)) * 80
    note = spill_inventory_note(body, ".monkeybot/spill/t/c.txt", tool_name="grep")
    assert "Spill inventory" in note
    assert "Preview:" in note
    assert "kind=" in note
    assert "tool=grep" in note
    assert ".monkeybot/spill/t/c.txt" in note
    assert body not in note
    assert len(note) < 3500


def test_spill_preview_code_uses_head_tail() -> None:
    lines = [f"def f{i}():\n    return {i}" for i in range(100)]
    text = "\n".join(lines)
    kind, preview, _unwrapped, _body_lines = _build_spill_preview(
        text, tool_name="read_file"
    )
    assert kind == "code"
    assert "def f0():" in preview
    assert "omitted from spill preview" in preview
    assert len(preview) < len(text)


def test_write_spill_sanitizes_path_traversal_thread_id(tmp_path: Path) -> None:
    from monkeybot.core.tools.spill_inventory import write_spill_with_inventory

    malicious = "../../../tmp/spill-escape"
    out = write_spill_with_inventory("payload", tmp_path, malicious, "call-1")
    assert ".." not in out
    spill_root = tmp_path / ".monkeybot" / "spill"
    written = list(spill_root.rglob("call-1.txt"))
    assert len(written) == 1
    assert spill_root in written[0].parents
    assert not (tmp_path.parent.parent / "tmp" / "spill-escape").exists()


def test_create_session_rejects_path_traversal_session_id() -> None:
    from pydantic import ValidationError

    from monkeybot.gateway.sse.models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(session_id="../../../tmp/x")


def test_sanitize_path_component_reserved_names() -> None:
    from monkeybot.core.path_safety import sanitize_path_component

    assert sanitize_path_component(".") == "_"
    assert sanitize_path_component("..") == "_"
    assert sanitize_path_component("") == "_"


def test_session_spill_dirs_dot_maps_to_safe_component(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    (root / "victim").mkdir(parents=True)
    (root / "victim" / "keep.txt").write_text("x")

    dirs = session_spill_dirs(tmp_path, ".")
    names = {p.name for p in dirs}
    assert names == {"_"}
    assert (root / "victim" / "keep.txt").is_file()


def test_create_session_rejects_dot_session_id() -> None:
    from pydantic import ValidationError

    from monkeybot.gateway.sse.models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(session_id=".")


def test_create_session_rejects_glob_session_id() -> None:
    from pydantic import ValidationError

    from monkeybot.gateway.sse.models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(session_id="*")


def test_write_spill_allows_symlinked_monkeybot_dir(tmp_path: Path) -> None:
    from monkeybot.core.tools.spill_inventory import write_spill_with_inventory

    ws = tmp_path / "ws"
    ws.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (ws / ".monkeybot").symlink_to(store)

    out = write_spill_with_inventory("payload", ws, "thread-1", "call-1")
    assert ".monkeybot/spill/thread-1/call-1.txt" in out
    written = list((store / "spill").rglob("call-1.txt"))
    assert len(written) == 1


def test_session_spill_dirs_skips_traversal_legacy_id(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    (root / "victim").mkdir(parents=True)
    (root / "victim" / "keep.txt").write_text("x")
    (root / "a___victim").mkdir(parents=True)

    dirs = session_spill_dirs(tmp_path, "a/../victim")
    names = {p.name for p in dirs}
    assert names == {"a___victim"}
    assert "victim" not in names
    assert (root / "victim" / "keep.txt").is_file()


def test_write_spill_skips_uncontained_underscore_fallback(tmp_path: Path) -> None:
    """A symlink at spill/_ must not become a write escape."""
    from monkeybot.core.tools.spill_inventory import write_spill_with_inventory

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    spill = ws / ".monkeybot" / "spill"
    spill.mkdir(parents=True)
    (spill / "_").symlink_to(outside, target_is_directory=True)
    # Force the sanitized dir itself to escape so the code takes the `_` fallback.
    (spill / "thread-1").symlink_to(outside, target_is_directory=True)

    out = write_spill_with_inventory("payload", ws, "thread-1", "call-1")
    assert not list(outside.rglob("*.txt"))
    assert "(unavailable)" in out or "payload" in out


def test_session_spill_dirs_legacy_subagent_prefix(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    legacy_name = "sess*1"
    (root / legacy_name).mkdir(parents=True)
    (root / f"subagent:{legacy_name}:x").mkdir(parents=True)
    (root / "sess_1").mkdir(parents=True)
    (root / "subagent:sess_1:y").mkdir(parents=True)

    dirs = session_spill_dirs(tmp_path, legacy_name)
    names = {p.name for p in dirs}
    assert names == {
        "sess_1",
        "subagent:sess_1:y",
        legacy_name,
        f"subagent:{legacy_name}:x",
    }
