"""Unit tests for monkeybot.core.persistence.transcript."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from monkeybot.core.path_safety import sanitize_path_component
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import (
    AssistantDelta,
    AssistantTextEnded,
    ContextUsage,
    SystemContextUpdated,
    SystemPromptSnapshot,
    ThinkingBlockComplete,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UsageTotals,
)


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_ensure_manifest_writes_line_one(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.ensure_manifest(agent_md="a.md", model="gpt-5", provider="openai")

    lines = _read_lines(writer.path)
    assert len(lines) == 1
    assert lines[0]["type"] == "SessionManifest"
    assert lines[0]["session_id"] == "sess-1"
    assert lines[0]["model"] == "gpt-5"
    assert "started_at" in lines[0]


@pytest.mark.asyncio
async def test_ensure_manifest_is_idempotent(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")
    await writer.ensure_manifest(model="gpt-5")
    await writer.ensure_manifest(model="gpt-5")

    lines = _read_lines(writer.path)
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_manifest_not_rewritten_across_new_writer_instances(tmp_path: Path) -> None:
    """Manifest is line 1 only; a second writer for the same session must not duplicate it."""
    w1 = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await w1.ensure_manifest(model="gpt-5")

    w2 = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await w2.ensure_manifest(model="gpt-5")

    lines = _read_lines(w1.path)
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_write_user_message(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.write_user_message(request_id="r1", content="hello there")

    lines = _read_lines(writer.path)
    assert lines[0]["type"] == "UserMessage"
    assert lines[0]["request_id"] == "r1"
    assert lines[0]["content"] == "hello there"
    assert lines[0]["seq"] == 1


@pytest.mark.asyncio
async def test_write_event_matches_sse_wire_shape(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.write_event(
        ToolCallStarted(request_id="r1", tool="read_file", label="Read x", args={"path": "x"})
    )
    await writer.write_event(TurnComplete(request_id="r1", usage=UsageTotals(input_tokens=5)))

    lines = _read_lines(writer.path)
    assert lines[0]["type"] == "ToolCallStarted"
    assert lines[0]["tool"] == "read_file"
    assert lines[0]["args"] == {"path": "x"}
    assert lines[1]["type"] == "TurnComplete"
    assert lines[1]["usage"]["input_tokens"] == 5


@pytest.mark.asyncio
async def test_write_event_skips_live_deltas(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.write_event(AssistantDelta(request_id="r1", delta="hi"))
    await writer.write_event(
        ToolCallStarted(request_id="r1", tool="read_file", label="Read x", args={"path": "x"})
    )
    lines = _read_lines(writer.path)
    assert len(lines) == 1
    assert lines[0]["type"] == "ToolCallStarted"


@pytest.mark.asyncio
async def test_write_provider_request_and_response(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=1,
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "read_file"}],
        thinking_budget=None,
    )
    await writer.write_provider_response(
        request_id="r1",
        inner_turn=1,
        model="gpt-5",
        text="hello",
        thinking="",
        tool_requests=[],
        usage={"input_tokens": 10, "output_tokens": 2},
    )

    lines = _read_lines(writer.path)
    assert lines[0]["type"] == "ProviderRequest"
    assert lines[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert lines[0]["tools"] == [{"name": "read_file"}]
    assert lines[0]["inner_turn"] == 1
    assert lines[0]["message_offset"] == 0
    assert lines[1]["type"] == "ProviderResponse"
    assert lines[1]["text"] == "hello"
    assert lines[1]["inner_turn"] == 1
    assert lines[1]["usage"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_write_provider_request_delta_only(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=1,
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "read_file"}],
        thinking_budget=None,
    )
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=2,
        model="gpt-5",
        messages=[
            {"role": "assistant", "content": "tool call"},
            {"role": "user", "content": "tool result"},
        ],
        message_offset=1,
        thinking_budget=None,
    )

    lines = _read_lines(writer.path)
    assert len(lines) == 2
    assert lines[1]["message_offset"] == 1
    assert len(lines[1]["messages"]) == 2
    assert "tools" not in lines[1]


@pytest.mark.asyncio
async def test_seq_is_monotonic_across_manifest_and_events(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-1", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")
    await writer.write_user_message(request_id="r1", content="hi")
    await writer.write_event(
        ToolCallStarted(request_id="r1", tool="read_file", label="Read x", args={"path": "x"})
    )

    lines = _read_lines(writer.path)
    assert "seq" not in lines[0]
    assert lines[1]["seq"] == 1
    assert lines[2]["seq"] == 2


@pytest.mark.asyncio
async def test_file_created_under_dot_monkeybot_transcripts(tmp_path: Path) -> None:
    writer = TranscriptWriter("abc-123", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")

    transcripts_root = tmp_path / ".monkeybot" / "transcripts"
    assert writer.path.parent.parent == transcripts_root
    assert writer.path.name == "transcript.ndjson"
    assert writer.session_dir.name.endswith("_abc-123")
    assert writer.path == writer.session_dir / "transcript.ndjson"
    assert writer.path.is_file()


@pytest.mark.asyncio
async def test_transcript_path_sanitizes_session_id(tmp_path: Path) -> None:
    """Path traversal in session_id must not escape the transcripts directory."""
    malicious = "../../../../tmp/pwned"
    writer = TranscriptWriter(malicious, workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")

    safe = sanitize_path_component(malicious)
    transcripts_root = tmp_path / ".monkeybot" / "transcripts"
    assert writer.path.parent.parent == transcripts_root
    assert writer.session_dir.name.endswith(f"_{safe}")
    assert writer.path.name == "transcript.ndjson"
    assert writer.path.is_file()
    assert not (tmp_path.parent.parent / "tmp" / "pwned.ndjson").exists()


@pytest.mark.asyncio
async def test_transcript_reuses_legacy_glob_session_dir(tmp_path: Path) -> None:
    """Pre-sanitization transcript folders must still be found after glob rewrite."""
    transcripts = tmp_path / ".monkeybot" / "transcripts"
    legacy = transcripts / "20260101T000000Z_sess*1"
    legacy.mkdir(parents=True)
    (legacy / "transcript.ndjson").write_text("{}\n", encoding="utf-8")

    writer = TranscriptWriter("sess*1", workspace_root=tmp_path)
    assert writer.session_dir == legacy


@pytest.mark.asyncio
async def test_second_writer_reuses_existing_session_dir(tmp_path: Path) -> None:
    w1 = TranscriptWriter("sess-reuse", workspace_root=tmp_path)
    await w1.ensure_manifest(model="gpt-5")
    w2 = TranscriptWriter("sess-reuse", workspace_root=tmp_path)
    assert w2.session_dir == w1.session_dir
    assert w2.path == w1.path


@pytest.mark.asyncio
async def test_second_writer_continues_seq_from_existing_file(tmp_path: Path) -> None:
    """Resumed writers must not emit duplicate seq evidence pointers."""
    w1 = TranscriptWriter("sess-reuse", workspace_root=tmp_path)
    await w1.ensure_manifest(model="gpt-5")
    await w1.write_user_message(request_id="r1", content="hello")
    await w1.write_event(
        ToolCallStarted(request_id="r1", tool="read_file", label="Read x", args={"path": "x"})
    )

    w2 = TranscriptWriter("sess-reuse", workspace_root=tmp_path)
    await w2.write_user_message(request_id="r2", content="again")

    lines = _read_lines(w1.path)
    seqs = [line["seq"] for line in lines if "seq" in line]
    assert seqs == [1, 2, 3]
    assert lines[-1]["type"] == "UserMessage"
    assert lines[-1]["request_id"] == "r2"


@pytest.mark.asyncio
async def test_manifest_documents_every_pointer_the_writer_emits(tmp_path: Path) -> None:
    """A reviewing agent opening the file cold must be able to resolve the stubs."""
    writer = TranscriptWriter("sess-format", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")

    documented = set(_read_lines(writer.path)[0]["format"])
    emitted = {"text_seq", "result_seq", "schema_seq", "content_seq", "base_seq", "diff", "changed"}
    assert emitted <= documented, f"undocumented pointers: {sorted(emitted - documented)}"


@pytest.mark.asyncio
async def test_ensure_manifest_appends_when_fingerprint_changes(tmp_path: Path) -> None:
    w1 = TranscriptWriter("sess-resume", workspace_root=tmp_path)
    await w1.ensure_manifest(model="gpt-5", provider="openai")
    w2 = TranscriptWriter("sess-resume", workspace_root=tmp_path)
    await w2.ensure_manifest(model="gpt-5-mini", provider="openai")

    lines = _read_lines(w1.path)
    manifests = [line for line in lines if line.get("type") == "SessionManifest"]
    assert len(manifests) == 2
    assert "resumed" not in manifests[0]
    assert manifests[1]["resumed"] is True
    assert manifests[1]["model"] == "gpt-5-mini"
    assert "seq" not in manifests[1]


@pytest.mark.asyncio
async def test_tool_schema_stub_points_at_full_schema_seq(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-schema", workspace_root=tmp_path)
    tools = [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}]
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=1,
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        thinking_budget=None,
        tools_include_reason="first_inner_turn",
    )
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=2,
        model="gpt-5",
        messages=[{"role": "user", "content": "again"}],
        message_offset=1,
        tools=tools,
        thinking_budget=None,
        tools_include_reason="tools_dirty",
        tools_dirty_reason="hook",
    )

    lines = _read_lines(writer.path)
    first, second = lines
    assert isinstance(first["tools"], list)
    stub = second["tools"]
    assert isinstance(stub, dict)
    assert stub["schema_seq"] == first["seq"]
    assert stub["tool_count"] == 1
    assert stub["names"] == ["read_file"]
    assert second["tools_include_reason"] == "tools_dirty"
    assert second["tools_dirty_reason"] == "hook"


@pytest.mark.asyncio
async def test_tool_response_stub_references_result_seq(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-result", workspace_root=tmp_path)
    await writer.write_event(
        ToolCallResult(
            request_id="r1",
            tool="glob",
            result="a" * 100,
            call_id="c1",
        )
    )
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=2,
        model="gpt-5",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "toolResponse",
                        "id": "c1",
                        "toolName": "glob",
                        "result": "a" * 100,
                        "isError": False,
                    }
                ],
            }
        ],
        thinking_budget=None,
    )

    lines = _read_lines(writer.path)
    result_line = next(line for line in lines if line["type"] == "ToolCallResult")
    req = next(line for line in lines if line["type"] == "ProviderRequest")
    block = req["messages"][0]["content"][0]
    assert block["result_seq"] == result_line["seq"]
    assert block["result_chars"] == len(json.dumps("a" * 100, ensure_ascii=False))
    assert "result" not in block


@pytest.mark.asyncio
async def test_system_message_points_at_system_prompt_snapshot(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-sysmsg", workspace_root=tmp_path)
    prompt = "# Identity\n" + "\n".join(f"rule {i}" for i in range(100))
    await writer.write_event(SystemPromptSnapshot(request_id="r1", inner_turn=1, text=prompt))
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=1,
        model="gpt-5",
        messages=[
            {"role": "system", "content": [{"type": "text", "text": prompt}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        thinking_budget=None,
    )

    lines = _read_lines(writer.path)
    snap = lines[0]
    # Stubbing is per block, so the original message shape survives replay.
    assert lines[1]["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text_seq": snap["seq"], "chars": len(prompt)}],
    }
    assert lines[1]["messages"][1]["content"] == [{"type": "text", "text": "hi"}]


@pytest.mark.asyncio
async def test_injected_context_update_points_at_its_event(tmp_path: Path) -> None:
    """The mid-epoch update is replayed verbatim in the next message body."""
    writer = TranscriptWriter("sess-scu", workspace_root=tmp_path)
    injected = "## System context update\n" + "\n".join(f"fact {i}" for i in range(60))
    await writer.write_event(
        SystemContextUpdated(
            request_id="r1", epoch_id=1, changed_sources=["current_request"], text=injected
        )
    )
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=2,
        model="gpt-5",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "toolResponse", "id": "c1", "toolName": "glob", "result": "x"},
                    {"type": "text", "text": injected},
                ],
            }
        ],
        thinking_budget=None,
    )

    event, request = _read_lines(writer.path)
    assert event["text"] == injected
    assert request["messages"][0]["content"][1] == {
        "type": "text",
        "text_seq": event["seq"],
        "chars": len(injected),
    }


@pytest.mark.asyncio
async def test_replayed_message_points_at_earlier_request(tmp_path: Path) -> None:
    """A new user turn replays history at offset 0; bodies are written once."""
    writer = TranscriptWriter("sess-replay", workspace_root=tmp_path)
    reply = {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]}
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=1,
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}, reply],
        thinking_budget=None,
    )
    await writer.write_provider_request(
        request_id="r2",
        inner_turn=1,
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}, reply, {"role": "user", "content": "more"}],
        thinking_budget=None,
    )

    first, second = _read_lines(writer.path)
    assert first["messages"][1] == reply
    assert second["messages"][1] == {
        "role": "assistant",
        "content_seq": first["seq"],
        "content_index": 1,
        "chars": len(json.dumps(reply["content"], ensure_ascii=False)),
    }
    # Short bodies stay inline — a pointer would cost more than the text.
    assert second["messages"][0]["content"] == "hi"
    assert second["messages"][2]["content"] == "more"


@pytest.mark.asyncio
async def test_tool_call_result_must_be_written_before_provider_request_stub(
    tmp_path: Path,
) -> None:
    """Stubbing is reference-based: the result record must already be in this file."""
    writer = TranscriptWriter("sess-order", workspace_root=tmp_path)
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "toolResponse",
                    "id": "c1",
                    "toolName": "glob",
                    "result": "blob",
                    "isError": False,
                }
            ],
        }
    ]
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=2,
        model="gpt-5",
        messages=messages,
        thinking_budget=None,
    )
    early = _read_lines(writer.path)[-1]
    assert early["messages"][0]["content"][0]["result"] == "blob"
    assert "result_seq" not in early["messages"][0]["content"][0]

    await writer.write_event(
        ToolCallResult(request_id="r1", tool="glob", result="blob", call_id="c1")
    )
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=3,
        model="gpt-5",
        messages=messages,
        thinking_budget=None,
    )
    late = _read_lines(writer.path)[-1]
    assert late["messages"][0]["content"][0]["result_seq"] is not None
    assert "result" not in late["messages"][0]["content"][0]


@pytest.mark.asyncio
async def test_hollow_assistant_text_ended_skipped_when_provider_records(
    tmp_path: Path,
) -> None:
    writer = TranscriptWriter("sess-sse", workspace_root=tmp_path, provider_records=True)
    await writer.ensure_manifest(model="gpt-5")
    await writer.write_event(AssistantTextEnded(request_id="r1", text="hello"))
    await writer.write_event(ThinkingBlockComplete(request_id="r1", signature=""))
    types = [line["type"] for line in _read_lines(writer.path)]
    assert types == ["SessionManifest"]


@pytest.mark.asyncio
async def test_assistant_text_ended_kept_when_realtime(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-rt", workspace_root=tmp_path, provider_records=False)
    await writer.write_event(AssistantTextEnded(request_id="r1", text="hello"))
    lines = _read_lines(writer.path)
    assert len(lines) == 1
    assert lines[0]["type"] == "AssistantTextEnded"
    assert lines[0]["text"] == "hello"


@pytest.mark.asyncio
async def test_empty_assistant_text_flagged_only_without_tool_requests(
    tmp_path: Path,
) -> None:
    writer = TranscriptWriter("sess-empty", workspace_root=tmp_path)
    for tool_requests in ([{"name": "glob"}], []):
        await writer.write_provider_response(
            request_id="r1",
            inner_turn=1,
            model="gpt-5",
            text="\n",
            thinking="",
            tool_requests=tool_requests,
            usage={},
        )
    tool_step, dead_end = _read_lines(writer.path)
    assert "assistant_text_empty" not in tool_step, "a tool step has no prose by design"
    assert dead_end["assistant_text_empty"] is True


@pytest.mark.asyncio
async def test_system_prompt_snapshot_full_once_then_hash(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-snap", workspace_root=tmp_path)
    snap = SystemPromptSnapshot(request_id="r1", inner_turn=1, text="## Agent\n\nYou are helpful.")
    await writer.write_event(snap)
    await writer.write_event(SystemPromptSnapshot(request_id="r1", inner_turn=2, text=snap.text))

    lines = _read_lines(writer.path)
    assert lines[0]["text"] == snap.text
    assert "changed" not in lines[0]
    assert "hash" in lines[0]
    assert "text" not in lines[1]
    assert lines[1]["changed"] is False
    assert lines[1]["base_seq"] == lines[0]["seq"]
    assert lines[1]["hash"] == lines[0]["hash"]


@pytest.mark.asyncio
async def test_drifting_system_prompt_written_as_diff_against_anchor(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-drift", workspace_root=tmp_path)
    body = "\n".join(f"line {i}" for i in range(200))
    first = f"# Agent\n\n{body}\n\n## Current request\nHello"
    second = f"# Agent\n\n{body}\n\n## Current request\nGoodbye"
    await writer.write_event(SystemPromptSnapshot(request_id="r1", inner_turn=1, text=first))
    await writer.write_event(SystemPromptSnapshot(request_id="r2", inner_turn=1, text=second))

    lines = _read_lines(writer.path)
    assert lines[0]["text"] == first
    assert "text" not in lines[1]
    assert lines[1]["changed"] is True
    assert lines[1]["base_seq"] == lines[0]["seq"]
    assert lines[1]["chars"] == len(second)
    assert any(line.startswith("+## Current request") is False for line in lines[1]["diff"])
    assert "+Goodbye" in lines[1]["diff"]
    assert "-Hello" in lines[1]["diff"]


def _apply_unified_diff(base: str, diff: list[str]) -> str:
    """Minimal unified-diff applier — what a reviewing agent does with ``base_seq``."""
    base_lines = base.split("\n")
    out: list[str] = []
    cursor = 0
    for line in diff:
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
            assert match is not None, line
            start = int(match.group(1)) - 1
            out.extend(base_lines[cursor:start])
            cursor = start
        elif line.startswith("-"):
            cursor += 1
        elif line.startswith("+"):
            out.append(line[1:])
        else:
            out.append(base_lines[cursor])
            cursor += 1
    out.extend(base_lines[cursor:])
    return "\n".join(out)


@pytest.mark.asyncio
async def test_system_prompt_diff_reconstructs_original_text(tmp_path: Path) -> None:
    """The transcript stays replay-grade: every stub resolves to the exact bytes."""
    writer = TranscriptWriter("sess-rebuild", workspace_root=tmp_path)
    body = "\n".join(f"line {i}" for i in range(200))
    # Real system prompts end with a newline, which ``str.splitlines`` would drop.
    texts = [
        f"# Agent\n\n{body}\n\n## Current request\nfirst\n",
        f"# Agent\n\n{body}\n\n## Current request\nsecond\n",
        f"# Agent\n\nprologue\n{body}\n\n## Current request\nthird\n",
    ]
    for turn, text in enumerate(texts, start=1):
        await writer.write_event(
            SystemPromptSnapshot(request_id=f"r{turn}", inner_turn=1, text=text)
        )

    lines = _read_lines(writer.path)
    by_seq = {line["seq"]: line for line in lines}

    def text_at(seq: int) -> str:
        record = by_seq[seq]
        if "text" in record:
            return record["text"]
        return _apply_unified_diff(text_at(record["base_seq"]), record["diff"])

    for line, expected in zip(lines, texts, strict=True):
        assert text_at(line["seq"]) == expected
        assert line["hash"] == lines[0]["hash"] or line["seq"] != lines[0]["seq"]
    assert "text" not in lines[1] and "text" not in lines[2], "only the anchor holds bytes"


@pytest.mark.asyncio
async def test_wholesale_system_prompt_change_re_anchors(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-reanchor", workspace_root=tmp_path)
    first = "\n".join(f"old {i}" for i in range(100))
    second = "\n".join(f"new {i}" for i in range(100))
    await writer.write_event(SystemPromptSnapshot(request_id="r1", inner_turn=1, text=first))
    await writer.write_event(SystemPromptSnapshot(request_id="r2", inner_turn=1, text=second))
    await writer.write_event(SystemPromptSnapshot(request_id="r3", inner_turn=1, text=second))

    lines = _read_lines(writer.path)
    assert lines[1]["text"] == second, "a diff larger than the body should re-anchor"
    assert "diff" not in lines[1]
    assert lines[2]["changed"] is False
    assert lines[2]["base_seq"] == lines[1]["seq"], "later records track the newest anchor"


@pytest.mark.asyncio
async def test_context_usage_written_as_extra_kind(tmp_path: Path) -> None:
    from monkeybot.core.runtime.events import DURABLE_EVENT_KINDS

    assert "ContextUsage" not in DURABLE_EVENT_KINDS
    writer = TranscriptWriter("sess-usage", workspace_root=tmp_path)
    await writer.write_event(
        ContextUsage(
            request_id="r1",
            estimated_tokens=40942,
            context_window_tokens=200000,
            inner_turn=2,
        )
    )
    lines = _read_lines(writer.path)
    assert lines[0]["type"] == "ContextUsage"
    assert lines[0]["estimated_tokens"] == 40942
    assert lines[0]["inner_turn"] == 2


@pytest.mark.asyncio
async def test_write_event_keeps_debug_fields_off_the_sse_shape(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess-debug", workspace_root=tmp_path)
    await writer.write_event(
        ToolCallStarted(
            request_id="r1",
            tool="read_file",
            label="read_file",
            args={"path": "notes.md"},
            call_id="c1",
            inspector_decision="allow",
            resource="notes.md",
            resolved_path="notes.md",
        )
    )
    await writer.write_event(
        ToolCallResult(
            request_id="r1",
            tool="read_file",
            result="ok",
            call_id="c1",
            error_kind="runtime",
            duration_ms=12,
        )
    )
    started, result = _read_lines(writer.path)
    assert started["inspector_decision"] == "allow"
    assert started["resource"] == "notes.md"
    assert started["resolved_path"] == "notes.md"
    assert result["error_kind"] == "runtime"
    assert result["duration_ms"] == 12
    assert "ok" not in result


@pytest.mark.asyncio
async def test_failed_append_does_not_advance_seq_or_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    writer = TranscriptWriter("sess-fail", workspace_root=tmp_path)
    await writer.write_event(
        ToolCallResult(request_id="r1", tool="t", result="ok", call_id="c1")
    )
    first_seq = writer._seq

    async def boom(_fn: object, *args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(asyncio, "to_thread", boom)
    await writer.write_event(
        ToolCallResult(request_id="r2", tool="t", result="ok", call_id="c2")
    )
    assert "c2" not in writer._result_seq_by_call_id
