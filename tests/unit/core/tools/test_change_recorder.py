"""Change recorder: snapshots, diffs stats, conflict, and undo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.change_recorder import (
    ChangeRevertError,
    ChangeStore,
    UndoNoteHook,
    capture_preimages,
    commit_file_changes,
)
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.patch import parse_patch, plan_and_apply_patch
from monkeybot.core.tools.workspace_service import WorkspaceFileService
from testkit.context import tool_ctx
from testkit.mcp import NoMCP
from testkit.memory import mem_sub


def _ws(root: Path) -> WorkspaceFileService:
    return WorkspaceFileService(root)


def test_write_then_replace_records_before_and_after(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("notes.md", "hello\n")
    pre = capture_preimages(ws, "replace_in_file", {"path": "notes.md", "old_string": "hello"})
    ws.replace_in_file("notes.md", "hello", "hello world")
    records = commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="call1")
    assert len(records) == 1
    record = records[0]
    assert record.action == "modify"
    assert record.additions == 1
    assert record.deletions == 1
    assert record.is_text is True
    content = ChangeStore(tmp_path).content("sess1", record.change_id)
    assert content is not None
    assert content["before"] == "hello\n"
    assert content["after"] == "hello world\n"


def test_create_undo_moves_file_to_trash(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "new.txt", "content": "hi"})
    ws.write_file("new.txt", "hi")
    records = commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="call1")
    assert records[0].action == "create"
    paths = ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert paths == ["new.txt"]
    assert not (tmp_path / "new.txt").exists()
    trash = list((tmp_path / ".monkeybot" / "changes" / "sess1" / "trash").rglob("new.txt"))
    assert len(trash) == 1
    assert trash[0].read_text(encoding="utf-8") == "hi"


def test_revert_conflict_without_force(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="c1")
    (tmp_path / "a.txt").write_text("edited by hand", encoding="utf-8")
    with pytest.raises(ChangeRevertError) as raised:
        ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert raised.value.code == "conflict"
    assert raised.value.paths == ["a.txt"]
    paths = ChangeStore(tmp_path).revert("sess1", request_id="req1", force=True)
    assert paths == ["a.txt"]
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_undo_note_is_drained_on_next_turn(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="c1")
    ChangeStore(tmp_path).revert("sess1", change_ids=None, request_id="req1")
    notes = ChangeStore(tmp_path).drain_notes("sess1")
    assert notes == ["User undid your changes to a.txt."]
    assert ChangeStore(tmp_path).drain_notes("sess1") == []

    hook = UndoNoteHook()
    manager = HookManager()
    hook.register(manager)
    pre_b = capture_preimages(ws, "write_file", {"path": "b.txt"})
    ws.write_file("b.txt", "x")
    commit_file_changes(ws, pre_b, session_id="sess1", request_id="req2", call_id="c2")
    ChangeStore(tmp_path).revert("sess1", request_id="req2")

    from monkeybot.core.context import TurnContext

    ctx = TurnContext(
        thread_id="sess1",
        request_id="next",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        workspace_root=tmp_path,
    )
    payload = HookPayload(event=HookEvent.PRE_TURN, thread_id="sess1", request_id="next", ctx=ctx)
    await manager.fire(payload)
    assert payload.inject_text == "User undid your changes to b.txt."


def test_list_marks_reverted(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    records = commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="c1")
    ChangeStore(tmp_path).revert("sess1", change_ids=[records[0].change_id])
    listed = ChangeStore(tmp_path).list_changes("sess1")
    assert listed[0]["reverted"] is True
    raw = (tmp_path / ".monkeybot" / "changes" / "sess1" / "log.jsonl").read_text(encoding="utf-8")
    assert json.loads(raw)["path"] == "a.txt"


_MOVE_PATCH = """*** Begin Patch
*** Update File: a.txt
*** Move to: b.txt
@@
-from
+moved
*** End Patch
"""

_MULTI_PATCH = """*** Begin Patch
*** Add File: new.txt
+added
*** Delete File: drop.txt
*** Update File: keep.txt
@@
-old
+new
*** End Patch
"""

_SWAP_PATCH = """*** Begin Patch
*** Delete File: swap.txt
*** Add File: swap.txt
+new
*** End Patch
"""


def _commit(ws: WorkspaceFileService, pre: list, *, request_id: str, call_id: str):
    return commit_file_changes(ws, pre, session_id="sess1", request_id=request_id, call_id=call_id)


def _log_entry(root: Path, session: str = "sess1") -> dict:
    path = root / ".monkeybot" / "changes" / session / "log.jsonl"
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def _write_log(root: Path, entry: dict, *, session: str = "sess1") -> None:
    path = root / ".monkeybot" / "changes" / session / "log.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def test_apply_patch_records_one_change_per_hunk(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("keep.txt", "old\n")
    ws.write_file("drop.txt", "bye\n")
    pre = capture_preimages(ws, "apply_patch", {"patch_text": _MULTI_PATCH})
    plan_and_apply_patch(ws, parse_patch(_MULTI_PATCH))
    records = _commit(ws, pre, request_id="req1", call_id="c1")
    assert [(record.path, record.action) for record in records] == [
        ("new.txt", "create"),
        ("drop.txt", "delete"),
        ("keep.txt", "modify"),
    ]
    assert records[2].additions == 1
    assert records[2].deletions == 1


def test_move_undo_restores_the_source_and_removes_the_destination(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("a.txt", "from\n")
    pre = capture_preimages(ws, "apply_patch", {"patch_text": _MOVE_PATCH})
    plan_and_apply_patch(ws, parse_patch(_MOVE_PATCH))
    records = _commit(ws, pre, request_id="req1", call_id="c1")
    assert records[0].action == "move"
    assert records[0].from_path == "a.txt"
    assert records[0].path == "b.txt"
    paths = ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert paths == ["b.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "from\n"
    assert not (tmp_path / "b.txt").exists()


def test_delete_undo_restores_the_original_bytes(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("gone.txt", "keep\n")
    patch = "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch\n"
    pre = capture_preimages(ws, "apply_patch", {"patch_text": patch})
    plan_and_apply_patch(ws, parse_patch(patch))
    records = _commit(ws, pre, request_id="req1", call_id="c1")
    assert records[0].action == "delete"
    paths = ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert paths == ["gone.txt"]
    assert (tmp_path / "gone.txt").read_text(encoding="utf-8") == "keep\n"


def test_delete_then_recreate_undo_restores_the_earliest_bytes(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("swap.txt", "old\n")
    pre = capture_preimages(ws, "apply_patch", {"patch_text": _SWAP_PATCH})
    plan_and_apply_patch(ws, parse_patch(_SWAP_PATCH))
    _commit(ws, pre, request_id="req1", call_id="c1")
    assert (tmp_path / "swap.txt").read_text(encoding="utf-8") == "new\n"
    ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert (tmp_path / "swap.txt").read_text(encoding="utf-8") == "old\n"


def test_binary_change_omits_bodies_and_still_undoes(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "bin.dat"})
    (tmp_path / "bin.dat").write_bytes(b"a\x00b")
    records = _commit(ws, pre, request_id="req1", call_id="c1")
    assert records[0].action == "create"
    assert records[0].is_text is False
    assert records[0].additions == 0
    assert records[0].deletions == 0
    content = ChangeStore(tmp_path).content("sess1", records[0].change_id)
    assert content is not None
    assert content["before"] is None
    assert content["after"] is None
    ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert not (tmp_path / "bin.dat").exists()


def test_oversized_before_and_after_refuse_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("monkeybot.core.tools.change_recorder.BLOB_CAP_BYTES", 8)
    ws = _ws(tmp_path)
    (tmp_path / "big.txt").write_bytes(b"x" * 32)
    pre = capture_preimages(ws, "replace_in_file", {"path": "big.txt"})
    (tmp_path / "big.txt").write_bytes(b"y" * 4)
    records = _commit(ws, pre, request_id="req-before", call_id="c1")
    assert records[0].too_large is True
    with pytest.raises(ChangeRevertError) as before_raised:
        ChangeStore(tmp_path).revert("sess1", request_id="req-before")
    assert before_raised.value.code == "too_large"

    pre_after = capture_preimages(ws, "write_file", {"path": "after.txt"})
    (tmp_path / "after.txt").write_bytes(b"z" * 32)
    after = _commit(ws, pre_after, request_id="req-after", call_id="c2")
    assert after[0].too_large is True
    assert after[0].before_sha is None
    with pytest.raises(ChangeRevertError) as after_raised:
        ChangeStore(tmp_path).revert("sess1", request_id="req-after")
    assert after_raised.value.code == "too_large"
    assert after_raised.value.paths == ["after.txt"]


def test_second_revert_of_the_same_request_is_a_no_op(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    _commit(ws, pre, request_id="req1", call_id="c1")
    store = ChangeStore(tmp_path)
    assert store.revert("sess1", request_id="req1") == ["a.txt"]
    assert store.drain_notes("sess1") == ["User undid your changes to a.txt."]
    assert store.revert("sess1", request_id="req1") == []
    assert store.drain_notes("sess1") == []


def test_unrecorded_later_edit_conflicts_and_keeps_those_bytes(tmp_path: Path) -> None:
    """A write the recorder never saw, such as ``sed -i``, must not be overwritten."""
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    _commit(ws, pre, request_id="req1", call_id="c1")
    (tmp_path / "a.txt").write_text("sed\n", encoding="utf-8")
    with pytest.raises(ChangeRevertError) as raised:
        ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert raised.value.code == "conflict"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "sed\n"


def test_unsafe_session_id_lists_empty_and_refuses_content_and_revert(tmp_path: Path) -> None:
    store = ChangeStore(tmp_path)
    assert store.list_changes("bad id") == []
    with pytest.raises(ValueError):
        store.content("bad id", "missing")
    with pytest.raises(ValueError):
        store.revert("bad id", request_id="req1")


@pytest.mark.asyncio
async def test_failed_replace_and_patch_record_nothing(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    executor = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=mem_sub(tmp_path / "mem"),
        skills_path=skills,
        mcp=NoMCP(),
    )
    ctx = tool_ctx()
    missed = await executor.execute(
        call=ToolCall(
            call_id="c1",
            name="replace_in_file",
            args={"path": "notes.txt", "old_string": "NO_SUCH", "new_string": "x"},
        ),
        ctx=ctx,
    )
    assert missed.error is not None
    assert missed.file_changes == ()
    rejected = await executor.execute(
        call=ToolCall(
            call_id="c2",
            name="apply_patch",
            args={
                "patch_text": (
                    "*** Begin Patch\n"
                    "*** Update File: notes.txt\n"
                    "@@\n"
                    "-not-the-line\n"
                    "+nope\n"
                    "*** End Patch\n"
                )
            },
        ),
        ctx=ctx,
    )
    assert rejected.error is not None
    assert rejected.file_changes == ()
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (tmp_path / ".monkeybot" / "changes").exists()


def test_undo_of_a_write_then_move_removes_both_paths(tmp_path: Path) -> None:
    """One request that creates a file and then moves it undoes back to nothing."""
    ws = _ws(tmp_path)
    pre_write = capture_preimages(ws, "write_file", {"path": "new.txt"})
    ws.write_file("new.txt", "body\n")
    _commit(ws, pre_write, request_id="req1", call_id="c1")
    patch = """*** Begin Patch
*** Update File: new.txt
*** Move to: b.txt
@@
-body
+body
*** End Patch
"""
    pre_move = capture_preimages(ws, "apply_patch", {"patch_text": patch})
    plan_and_apply_patch(ws, parse_patch(patch))
    _commit(ws, pre_move, request_id="req1", call_id="c2")
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "b.txt").is_file()
    ChangeStore(tmp_path).revert("sess1", request_id="req1")
    assert not (tmp_path / "new.txt").exists()
    assert not (tmp_path / "b.txt").exists()


def test_revert_refuses_a_log_path_outside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("keep\n", encoding="utf-8")
    ws = _ws(root)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one\n")
    records = commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="c1")
    entry = _log_entry(root)
    entry["path"] = "../secret.txt"
    entry["action"] = "modify"
    entry["before_sha"] = entry["after_sha"]
    _write_log(root, entry)
    ChangeStore(root).revert("sess1", change_ids=[records[0].change_id], force=True)
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_content_refuses_a_blob_name_outside_the_store(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret-bytes", encoding="utf-8")
    ws = _ws(root)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one\n")
    records = commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="c1")
    entry = _log_entry(root)
    entry["before_sha"] = "../../../../../secret.txt"
    entry["is_text"] = True
    entry["too_large"] = False
    _write_log(root, entry)
    payload = ChangeStore(root).content("sess1", records[0].change_id)
    assert payload is not None
    rendered = json.dumps(payload)
    assert "secret-bytes" not in rendered


def test_revert_keeps_trash_inside_the_session_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    ws = _ws(root)
    pre = capture_preimages(ws, "write_file", {"path": "new.txt"})
    ws.write_file("new.txt", "hi")
    commit_file_changes(ws, pre, session_id="sess1", request_id="req1", call_id="c1")
    entry = _log_entry(root)
    entry["change_id"] = "../../escaped"
    _write_log(root, entry)
    ChangeStore(root).revert("sess1", request_id="req1")
    session = root / ".monkeybot" / "changes" / "sess1"
    found = list((root / ".monkeybot").rglob("new.txt"))
    assert found
    assert all(session in path.parents for path in found)


def test_a_torn_log_line_does_not_hide_earlier_changes(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    _commit(ws, pre, request_id="req1", call_id="c1")
    log = tmp_path / ".monkeybot" / "changes" / "sess1" / "log.jsonl"
    log.write_text(log.read_text(encoding="utf-8") + '{"change_id":\n', encoding="utf-8")
    listed = ChangeStore(tmp_path).list_changes("sess1")
    assert [entry["path"] for entry in listed] == ["a.txt"]


def test_reverting_an_earlier_edit_does_not_let_a_later_one_reapply_it(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("a.txt", "base\n")
    pre_first = capture_preimages(ws, "replace_in_file", {"path": "a.txt"})
    ws.replace_in_file("a.txt", "base", "mid")
    first = _commit(ws, pre_first, request_id="r1", call_id="c1")
    pre_second = capture_preimages(ws, "replace_in_file", {"path": "a.txt"})
    ws.replace_in_file("a.txt", "mid", "end")
    second = _commit(ws, pre_second, request_id="r2", call_id="c2")
    store = ChangeStore(tmp_path)
    store.revert("sess1", change_ids=[first[0].change_id], force=True)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "base\n"
    store.revert("sess1", change_ids=[second[0].change_id], force=True)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "base\n"


def test_size_cap_does_not_delete_the_session_being_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("monkeybot.core.tools.change_recorder.WORKSPACE_CAP_BYTES", 1)
    ws = _ws(tmp_path)
    pre = capture_preimages(ws, "write_file", {"path": "a.txt"})
    ws.write_file("a.txt", "one")
    _commit(ws, pre, request_id="req1", call_id="c1")
    log = tmp_path / ".monkeybot" / "changes" / "sess1" / "log.jsonl"
    assert log.is_file()
