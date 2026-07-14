"""Unit tests for monkeybot.core.persistence.transcript_analyzer."""

from __future__ import annotations

import json
from pathlib import Path

from monkeybot.core.persistence.transcript_analyzer import (
    analyze_records,
    analyze_transcript,
    reconstruct_messages,
)


def _write_ndjson(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _sample_records() -> list[dict[str, object]]:
    return [
        {
            "type": "SessionManifest",
            "session_id": "sess-a",
            "started_at": "2026-07-14T15:00:00.000Z",
            "model": "gpt-test",
            "provider": "fake",
            "workspace_root": "/tmp/ws",
            "durable_only": True,
        },
        {
            "seq": 1,
            "ts": "2026-07-14T15:00:01.000Z",
            "type": "UserMessage",
            "request_id": "r1",
            "content": "hello",
        },
        {
            "seq": 2,
            "ts": "2026-07-14T15:00:01.050Z",
            "type": "ContextEpochStarted",
            "request_id": "r1",
            "epoch_id": 1,
            "changed_sources": ["epoch"],
        },
        {
            "seq": 3,
            "ts": "2026-07-14T15:00:01.100Z",
            "type": "ProviderRequest",
            "request_id": "r1",
            "inner_turn": 1,
            "model": "gpt-test",
            "message_offset": 0,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "read_file", "parameters": {"type": "object"}}],
            "thinking_budget": None,
        },
        {
            "seq": 4,
            "ts": "2026-07-14T15:00:02.050Z",
            "type": "ThinkingBlockComplete",
            "request_id": "r1",
            "signature": "",
        },
        {
            "seq": 5,
            "ts": "2026-07-14T15:00:02.080Z",
            "type": "AssistantTextEnded",
            "request_id": "r1",
            "text": "",
        },
        {
            "seq": 6,
            "ts": "2026-07-14T15:00:02.100Z",
            "type": "ProviderResponse",
            "request_id": "r1",
            "inner_turn": 1,
            "model": "gpt-test",
            "text": "",
            "thinking": "",
            "tool_requests": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 0},
        },
        {
            "seq": 7,
            "ts": "2026-07-14T15:00:02.200Z",
            "type": "ToolCallStarted",
            "request_id": "r1",
            "call_id": "c1",
            "tool": "read_file",
            "args": {"path": "a"},
        },
        {
            "seq": 8,
            "ts": "2026-07-14T15:00:02.500Z",
            "type": "ToolCallResult",
            "request_id": "r1",
            "call_id": "c1",
            "tool": "read_file",
            "result": "ok",
        },
        {
            "seq": 9,
            "ts": "2026-07-14T15:00:02.600Z",
            "type": "ProviderRequest",
            "request_id": "r1",
            "inner_turn": 2,
            "model": "gpt-test",
            "message_offset": 1,
            "messages": [
                {"role": "assistant", "content": "tool"},
                {"role": "user", "content": "result"},
            ],
            "thinking_budget": None,
        },
        {
            "seq": 10,
            "ts": "2026-07-14T15:00:03.000Z",
            "type": "ProviderResponse",
            "request_id": "r1",
            "inner_turn": 2,
            "model": "gpt-test",
            "text": "",
            "thinking": "",
            "tool_requests": [],
            "usage": {"input_tokens": 12, "output_tokens": 0, "cached_tokens": 0},
        },
        {
            "seq": 11,
            "ts": "2026-07-14T15:00:03.100Z",
            "type": "TurnComplete",
            "request_id": "r1",
            "usage": {
                "input_tokens": 22,
                "output_tokens": 5,
                "cached_tokens": 0,
                "duration_ms": 2100,
            },
        },
        # Cancelled / errored second turn
        {
            "seq": 12,
            "ts": "2026-07-14T15:00:04.000Z",
            "type": "UserMessage",
            "request_id": "r2",
            "content": "again",
        },
        {
            "seq": 13,
            "ts": "2026-07-14T15:00:04.050Z",
            "type": "Error",
            "request_id": "r2",
            "error": "Request cancelled",
        },
        {
            "seq": 14,
            "ts": "2026-07-14T15:00:04.100Z",
            "type": "TurnComplete",
            "request_id": "r2",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "duration_ms": 100,
            },
        },
        # Tool thrash (3 identical calls) on a third turn
        {
            "seq": 15,
            "ts": "2026-07-14T15:00:05.000Z",
            "type": "UserMessage",
            "request_id": "r3",
            "content": "retry",
        },
        {
            "seq": 16,
            "ts": "2026-07-14T15:00:05.100Z",
            "type": "ToolCallStarted",
            "request_id": "r3",
            "call_id": "t1",
            "tool": "bash",
            "args": {"cmd": "ls"},
        },
        {
            "seq": 17,
            "ts": "2026-07-14T15:00:05.200Z",
            "type": "ToolCallResult",
            "request_id": "r3",
            "call_id": "t1",
            "tool": "bash",
            "error": "command not on allowlist",
        },
        {
            "seq": 18,
            "ts": "2026-07-14T15:00:05.300Z",
            "type": "ToolCallStarted",
            "request_id": "r3",
            "call_id": "t2",
            "tool": "bash",
            "args": {"cmd": "ls"},
        },
        {
            "seq": 19,
            "ts": "2026-07-14T15:00:05.400Z",
            "type": "ToolCallResult",
            "request_id": "r3",
            "call_id": "t2",
            "tool": "bash",
            "error": "command not on allowlist",
        },
        {
            "seq": 20,
            "ts": "2026-07-14T15:00:05.500Z",
            "type": "ToolCallStarted",
            "request_id": "r3",
            "call_id": "t3",
            "tool": "bash",
            "args": {"cmd": "ls"},
        },
        {
            "seq": 21,
            "ts": "2026-07-14T15:00:05.600Z",
            "type": "ToolCallResult",
            "request_id": "r3",
            "call_id": "t3",
            "tool": "bash",
            "error": "command not on allowlist",
        },
        {
            "seq": 22,
            "ts": "2026-07-14T15:00:05.700Z",
            "type": "TurnComplete",
            "request_id": "r3",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cached_tokens": 0,
                "duration_ms": 700,
            },
        },
    ]


def test_reconstruct_messages_delta_encoding() -> None:
    records = _sample_records()
    rebuilt = reconstruct_messages(records)
    assert ("r1", 1) in rebuilt
    assert ("r1", 2) in rebuilt
    assert len(rebuilt[("r1", 1)]) == 1
    assert len(rebuilt[("r1", 2)]) == 3
    assert rebuilt[("r1", 2)][0]["role"] == "user"


def test_analyze_records_timeline_perf_and_smells() -> None:
    analysis = analyze_records(_sample_records())
    assert analysis.session_id == "sess-a"
    assert analysis.turn_count == 3
    assert analysis.cancelled_turn_count == 1
    assert analysis.inner_turn_count == 2

    # Wall time for r1 ≈ 2100ms
    r1_wall = next(t for t in analysis.wall_times_ms if t["request_id"] == "r1")
    assert r1_wall["duration_ms"] == 2100
    assert r1_wall["status"] == "ok"
    r2_wall = next(t for t in analysis.wall_times_ms if t["request_id"] == "r2")
    assert r2_wall["status"] == "error"

    kinds = {f.kind for f in analysis.findings}
    assert "empty_post_tool_reply" in kinds
    assert "tool_loop_thrash" in kinds
    assert "policy_vs_execution" in kinds
    assert "error_clustering" in kinds

    timeline_types = {e["type"] for e in analysis.timeline}
    assert "ContextEpochStarted" in timeline_types
    assert "ThinkingBlockComplete" in timeline_types
    assert "AssistantTextEnded" in timeline_types

    # Enlarge tools so token_waste fires, then check evidence type is ProviderRequest
    records = _sample_records()
    for rec in records:
        if rec.get("type") == "ProviderRequest" and rec.get("tools"):
            rec["tools"] = [{"name": f"t{i}", "parameters": {"type": "object", "x": "y" * 400}} for i in range(30)]
    waste = analyze_records(records)
    tw = next(f for f in waste.findings if f.kind == "token_waste")
    assert tw.evidence[0]["type"] == "ProviderRequest"
    assert tw.evidence[0]["seq"] is not None


def test_analyze_transcript_writes_artifacts(tmp_path: Path) -> None:
    session_dir = tmp_path / "20260714T150000Z_sess-a"
    ndjson = session_dir / "transcript.ndjson"
    _write_ndjson(ndjson, _sample_records())

    out = analyze_transcript(ndjson)
    assert out == session_dir
    assert (session_dir / "brief.md").is_file()
    assert (session_dir / "report.json").is_file()
    assert (session_dir / "meta.json").is_file()

    brief = (session_dir / "brief.md").read_text(encoding="utf-8")
    assert "## Session summary" in brief
    assert "## Suspected harness issues (ranked)" in brief

    report = json.loads((session_dir / "report.json").read_text(encoding="utf-8"))
    assert report["session_id"] == "sess-a"
    assert "scorecard" in report
    assert report["scorecard"]["cancelled_turn_count"] == 1

    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["session_id"] == "sess-a"
    assert "analyzed_at" in meta
    assert meta["harness"]["package"] == "monkeybot"


def test_analyze_transcript_missing_or_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "transcript.ndjson"
    assert analyze_transcript(missing) is None

    empty = tmp_path / "empty" / "transcript.ndjson"
    empty.parent.mkdir(parents=True)
    empty.write_text("", encoding="utf-8")
    assert analyze_transcript(empty) is None
