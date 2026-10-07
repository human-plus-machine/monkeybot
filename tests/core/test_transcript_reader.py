"""Resolve stubbed transcripts and build a retro digest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.persistence.transcript import TranscriptWriter, _text_diff
from monkeybot.core.persistence.transcript_reader import (
    build_digest,
    load_records,
    record_by_seq,
    render_digest_markdown,
    resolve_records,
    scan_session_signals,
)
from monkeybot.core.runtime.events import SystemPromptSnapshot, ToolCallResult


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

    assert record_by_seq(session, 6)["detail"] == "stuck on grep"
    assert scan_session_signals(session).score() == digest.signals.score()
