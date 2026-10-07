"""Resolve stubbed transcripts and build a retro digest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.persistence.transcript import (
    TranscriptWriter,
    _text_diff,
    start_child_transcript,
)
from monkeybot.core.persistence.transcript_reader import (
    SubagentBlock,
    _task_child,
    apply_unified_diff,
    build_digest,
    load_records,
    records_by_seq,
    render_digest_markdown,
    resolve_records,
    scan_session_signals,
    signals_from_records,
)
from monkeybot.core.runtime.events import SystemPromptSnapshot, ToolCallResult, ToolCallStarted
from monkeybot.core.tools.core_tool_executor import _task_result_payload


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_resolve_expands_diff_schema_content_and_result(tmp_path: Path) -> None:
    base = "\n".join(f"rule {i} stays the same for the cache prefix" for i in range(80))
    changed = base.replace(
        "rule 10 stays the same for the cache prefix",
        "rule 10 CHANGED for the cache prefix",
        1,
    )
    assert _text_diff(base, changed) is not None
    big_result = "R" * 300
    tools = [{"name": "read_file", "description": "read a file"}]
    content = [
        {"type": "text", "text": base},
        {
            "type": "toolResponse",
            "id": "c1",
            "toolName": "read_file",
            "isError": False,
            "result": big_result,
        },
    ]
    message = {"role": "user", "content": content}

    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.write_event(SystemPromptSnapshot(request_id="r", inner_turn=1, text=base))
    await writer.write_event(SystemPromptSnapshot(request_id="r", inner_turn=2, text=changed))
    await writer.write_event(
        ToolCallResult(request_id="r", tool="read_file", result=big_result, call_id="c1")
    )
    await writer.write_provider_request(
        request_id="r",
        inner_turn=1,
        model="m",
        messages=[message],
        tools=tools,
        thinking_budget=None,
    )
    await writer.write_provider_request(
        request_id="r",
        inner_turn=2,
        model="m",
        messages=[message],
        tools=tools,
        thinking_budget=None,
    )

    raw = load_records(writer.path)
    snapshots = [row for row in raw if row.get("type") == "SystemPromptSnapshot"]
    assert "diff" in snapshots[1]
    assert "text" not in snapshots[1]
    requests = [row for row in raw if row.get("type") == "ProviderRequest"]
    assert isinstance(requests[1]["tools"], dict)
    assert "content_seq" in requests[1]["messages"][0]

    resolved = resolve_records(raw)
    resolved_snaps = [row for row in resolved if row.get("type") == "SystemPromptSnapshot"]
    assert resolved_snaps[1]["text"] == changed
    resolved_reqs = [row for row in resolved if row.get("type") == "ProviderRequest"]
    assert resolved_reqs[1]["tools"] == tools
    restored = resolved_reqs[1]["messages"][0]["content"]
    assert restored[0]["text"] == base
    assert restored[1]["result"] == big_result


def test_load_records_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "transcript.ndjson"
    path.write_text('{"seq":1,"type":"UserMessage","content":"hi"}\nnot-json\n', encoding="utf-8")
    records = load_records(path)
    assert len(records) == 1
    assert records[0]["content"] == "hi"


def test_load_records_raises_when_read_fails(tmp_path: Path) -> None:
    path = tmp_path / "transcript.ndjson"
    path.write_text('{"seq":1,"type":"UserMessage","content":"hi"}\n', encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(OSError):
            load_records(path)
    finally:
        path.chmod(0o644)


def test_digest_signals_timeline_and_subagent(tmp_path: Path) -> None:
    session = tmp_path / "20260101T000000Z_parent"
    child_id = "subagent:parent:abc"
    _write(
        session / "transcript.ndjson",
        [
            {"type": "SessionManifest", "session_id": "parent", "model": "m", "provider": "fake"},
            {"seq": 1, "type": "UserMessage", "content": "find the bug"},
            {
                "seq": 2,
                "type": "ToolCallStarted",
                "tool": "grep",
                "call_id": "g1",
                "args": {"pattern": "bug"},
            },
            {
                "seq": 3,
                "type": "ToolCallResult",
                "tool": "grep",
                "call_id": "g1",
                "result": "nope",
                "error": "not found",
                "error_kind": "not_found",
                "duration_ms": 12000,
            },
            {
                "seq": 4,
                "type": "ToolCallStarted",
                "tool": "grep",
                "call_id": "g2",
                "args": {"pattern": "bug"},
            },
            {
                "seq": 5,
                "type": "ToolCallResult",
                "tool": "grep",
                "call_id": "g2",
                "result": "still nope",
                "error": "not found",
                "error_kind": "not_found",
                "duration_ms": 20,
            },
            {
                "seq": 6,
                "type": "HarnessIntervention",
                "intervention": "doom_loop",
                "detail": "stuck on grep",
                "inner_turn": 2,
            },
            {"seq": 7, "type": "UserSteered", "text": "try read_file"},
            {"seq": 8, "type": "ContextSummarized", "turns_summarized": 4},
            {
                "seq": 9,
                "type": "ProviderResponse",
                "text": "",
                "tool_requests": [],
                "assistant_text_empty": True,
            },
            {
                "seq": 10,
                "type": "SubagentStarted",
                "child_thread_id": child_id,
                "subagent_type": "explore",
            },
            {
                "seq": 11,
                "type": "ProviderResponse",
                "text": "I looked in src/app.py",
                "tool_requests": [],
            },
            {"seq": 12, "type": "VerifierVerdict", "status": "drifting", "severity": "medium"},
        ],
    )
    from monkeybot.core.persistence.transcript import subagent_transcript_dir

    child = subagent_transcript_dir(session, child_id)
    _write(
        child / "transcript.ndjson",
        [
            {"type": "SessionManifest", "session_id": child_id, "subagent_type": "explore"},
            {"seq": 1, "type": "UserMessage", "content": "search the repo"},
            {
                "seq": 2,
                "type": "ToolCallResult",
                "tool": "read_file",
                "call_id": "r1",
                "result": "",
                "error": "missing",
                "error_kind": "missing",
                "duration_ms": 5,
            },
        ],
    )

    digest = build_digest(session)
    assert digest.signals.tool_errors["not_found"] == 2
    assert digest.signals.tool_errors["missing"] == 1
    assert digest.signals.interventions["doom_loop"] == 1
    assert digest.signals.steers == 1
    assert digest.signals.verdicts == 1
    assert digest.signals.empty_assistant == 1
    assert digest.signals.summaries == 1
    assert digest.signals.slow_tools[0].tool == "grep"
    assert digest.signals.repeated_calls[0].count == 2
    assert digest.turns[0].user_text == "find the bug"
    assert digest.turns[0].final_text == "I looked in src/app.py"
    subagents = [
        item for item in digest.turns[0].items if item.__class__.__name__ == "SubagentBlock"
    ]
    assert subagents[0].digest is not None
    assert subagents[0].digest.turns[0].user_text == "search the repo"

    markdown = render_digest_markdown(digest, max_chars=0)
    assert "@seq 6" in markdown
    assert "doom_loop" in markdown
    assert "search the repo" in markdown
    capped = render_digest_markdown(digest, max_chars=80)
    assert "truncated" in capped
    assert len(capped) <= 80

    found = records_by_seq(session, [6, 7, 999])
    assert found[6]["detail"] == "stuck on grep"
    assert found[7]["text"] == "try read_file"
    assert 999 not in found
    assert scan_session_signals(session).score() == digest.signals.score()


@pytest.mark.parametrize(
    "removed_or_added",
    [("---", None), ("-- sql comment", None), (None, "++counter"), (None, "+++ header-like")],
)
@pytest.mark.asyncio
async def test_prompt_diff_round_trips_dash_and_plus_lines(
    tmp_path: Path, removed_or_added: tuple[str | None, str | None]
) -> None:
    removed, added = removed_or_added
    filler = [f"rule {i} stays the same for the cache prefix" for i in range(80)]
    base_lines = [*filler[:40], *([removed] if removed else []), *filler[40:]]
    new_lines = [*filler[:40], *([added] if added else []), *filler[40:]]
    base, changed = "\n".join(base_lines), "\n".join(new_lines)

    writer = TranscriptWriter("sess-diff", workspace_root=tmp_path)
    await writer.write_event(SystemPromptSnapshot(request_id="r", inner_turn=1, text=base))
    await writer.write_event(SystemPromptSnapshot(request_id="r", inner_turn=2, text=changed))

    raw = load_records(writer.path)
    assert "diff" in raw[-1]
    resolved = resolve_records(raw)
    assert resolved[-1]["text"] == changed


def test_diff_from_old_writer_is_flagged_not_misapplied() -> None:
    base = "\n".join(["a", "b", "---", "c", "d"])
    # Old writers filtered out the removed "----" line along with the file header.
    old_writer_diff = ["@@ -2,3 +2,2 @@", " b", " c"]
    with pytest.raises(ValueError, match="expected 3 / 2"):
        apply_unified_diff(base, old_writer_diff)

    records = [
        {"seq": 1, "type": "SystemPromptSnapshot", "text": base},
        {
            "seq": 2,
            "type": "SystemPromptSnapshot",
            "base_seq": 1,
            "changed": True,
            "diff": old_writer_diff,
        },
    ]
    resolved = resolve_records(records)
    assert "text" not in resolved[1]
    assert resolved[1]["text_error"].startswith("diff_mismatch")


def test_pure_insertion_hunk_lands_after_named_line() -> None:
    assert apply_unified_diff("a\nb\nc", ["@@ -2,0 +3 @@", "+new"]) == "a\nb\nnew\nc"
    assert apply_unified_diff("a\nb", ["@@ -0,0 +1 @@", "+top"]) == "top\na\nb"


@pytest.mark.asyncio
async def test_task_result_links_child_written_by_real_writers(tmp_path: Path) -> None:
    parent = TranscriptWriter("parent-7", workspace_root=tmp_path)
    await parent.write_user_message(request_id="r1", content="investigate")
    child_id = "subagent:parent-7:abc123"
    await parent.write_event(
        ToolCallStarted(
            request_id="r1", tool="task", call_id="t1", args={"task": "search the repo"}
        )
    )
    payload = _task_result_payload(
        errors=[],
        deltas=["found it"],
        tool_call_count=1,
        tool_results=[],
        turn_complete=None,
        scratch=tmp_path / "scratch",
        run_id="run-1",
        child_thread_id=child_id,
        subagent_type="explore",
    )
    await parent.write_event(
        ToolCallResult(request_id="r1", tool="task", call_id="t1", result=json.dumps(payload))
    )
    await parent.drain()

    child = await start_child_transcript(
        workspace_root=tmp_path,
        parent_session_id="parent-7",
        child_thread_id=child_id,
        request_id="sub-1",
        task="search the repo",
        model="m",
        provider="fake",
        agent_md="AGENT.md",
        subagent_type="explore",
        parent_run_id="r1:t1",
    )
    await child.write_event(
        ToolCallResult(
            request_id="sub-1", tool="read_file", call_id="c1", error="missing", error_kind="nf"
        )
    )
    await child.drain()
    assert child.path.parent.parent.parent == parent.session_dir

    digest = build_digest(parent.session_dir)
    blocks = [item for item in digest.turns[0].items if isinstance(item, SubagentBlock)]
    assert len(blocks) == 1
    assert blocks[0].child_thread_id == child_id
    assert blocks[0].subagent_type == "explore"
    assert blocks[0].digest is not None
    assert blocks[0].digest.turns[0].user_text == "search the repo"
    assert digest.unlinked_subagents == []
    assert digest.signals.tool_errors == {"nf": 1}
    assert scan_session_signals(parent.session_dir).score() == digest.signals.score()
    assert "search the repo" in render_digest_markdown(digest)


def test_clipped_task_result_still_links_child() -> None:
    started = {"tool": "task"}
    clipped = {"result": '{"ok": true, "child_thread_id": "subagent:p:x1", "final_message": "…'}
    assert _task_child(started, clipped) == ("subagent:p:x1", None)
    assert _task_child({"tool": "grep"}, clipped) is None


def test_unreferenced_child_is_listed_and_scored(tmp_path: Path) -> None:
    session = tmp_path / "20260101T000000Z_parent"
    _write(
        session / "transcript.ndjson",
        [{"seq": 1, "type": "UserMessage", "content": "go"}],
    )
    _write(
        session / "subagents" / "queued-child" / "transcript.ndjson",
        [
            {"type": "SessionManifest", "subagent_type": "worker"},
            {"seq": 1, "type": "UserMessage", "content": "queued work"},
            {"seq": 2, "type": "ToolCallResult", "tool": "grep", "call_id": "g", "error": "x"},
        ],
    )
    digest = build_digest(session)
    assert [orphan.dir_name for orphan in digest.unlinked_subagents] == ["queued-child"]
    assert digest.signals.tool_errors == {"error": 1}
    assert scan_session_signals(session).score() == digest.signals.score()
    markdown = render_digest_markdown(digest)
    assert "Unlinked subagent runs" in markdown
    assert "queued work" in markdown


def test_repeated_calls_are_scoped_to_one_turn() -> None:
    def call(seq: int, call_id: str) -> dict[str, object]:
        return {
            "seq": seq,
            "type": "ToolCallStarted",
            "tool": "read_file",
            "call_id": call_id,
            "args": {"path": "a.py"},
        }

    records = [
        {"seq": 1, "type": "UserMessage", "content": "one"},
        call(2, "a"),
        {"seq": 3, "type": "UserMessage", "content": "two"},
        call(4, "b"),
        call(5, "c"),
    ]
    repeated = signals_from_records(records).repeated_calls
    assert len(repeated) == 1
    assert repeated[0].count == 2
    assert repeated[0].first_seq == 4


def test_truncated_batch_counts_once() -> None:
    records = [
        {"seq": 1, "type": "UserMessage", "content": "go"},
        {"seq": 2, "type": "HarnessIntervention", "intervention": "truncated_batch"},
        {"seq": 3, "type": "ToolCallStarted", "tool": "write_file", "call_id": "w1", "args": {}},
        {"seq": 4, "type": "ToolCallResult", "tool": "write_file", "call_id": "w1", "error": "x"},
        {"seq": 5, "type": "ToolCallStarted", "tool": "write_file", "call_id": "w2", "args": {}},
        {"seq": 6, "type": "ToolCallResult", "tool": "write_file", "call_id": "w2", "error": "x"},
        {"seq": 7, "type": "ProviderResponse", "text": ""},
        {"seq": 8, "type": "ToolCallStarted", "tool": "write_file", "call_id": "w3", "args": {}},
        {"seq": 9, "type": "ToolCallResult", "tool": "write_file", "call_id": "w3", "error": "y"},
    ]
    signals = signals_from_records(records)
    assert signals.interventions == {"truncated_batch": 1}
    assert signals.tool_errors == {"error": 1}
    assert signals.repeated_calls == []
    assert signals.score() == 2


def test_unfinished_call_keeps_seq_order(tmp_path: Path) -> None:
    session = tmp_path / "s"
    _write(
        session / "transcript.ndjson",
        [
            {"seq": 1, "type": "UserMessage", "content": "go"},
            {"seq": 2, "type": "ToolCallStarted", "tool": "slow", "call_id": "s1", "args": {}},
            {"seq": 3, "type": "ToolCallStarted", "tool": "fast", "call_id": "f1", "args": {}},
            {"seq": 4, "type": "ToolCallResult", "tool": "fast", "call_id": "f1", "result": "ok"},
            {"seq": 5, "type": "UserSteered", "text": "stop"},
        ],
    )
    items = build_digest(session).turns[0].items
    assert [item.seq for item in items] == [2, 4, 5]
    assert items[0].error_kind == "unfinished"
