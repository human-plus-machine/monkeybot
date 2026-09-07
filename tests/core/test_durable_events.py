"""Tests for durable vs live-only event taxonomy (P4.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import (
    DURABLE_EVENT_KINDS,
    AssistantDelta,
    AssistantTextEnded,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    event_from_json,
    event_to_json,
    is_durable_event,
)


def test_is_durable_event_classifies_settlement() -> None:
    assert is_durable_event(ToolCallResult(request_id="r", tool="t", result="ok"))
    assert is_durable_event(ToolCallStarted(request_id="r", tool="t", label="t"))
    assert is_durable_event(TurnComplete(request_id="r"))
    assert not is_durable_event(AssistantDelta(request_id="r", delta="hi"))
    assert "AssistantDelta" not in DURABLE_EVENT_KINDS
    from monkeybot.core.runtime.events import VerifierVerdict

    assert is_durable_event(
        VerifierVerdict(request_id="r", verdict_id="v", status="drifting", severity="none")
    )
    assert "VerifierVerdict" in DURABLE_EVENT_KINDS


def test_subagent_lifecycle_durable_classification() -> None:
    from monkeybot.core.runtime.events import (
        SubagentCompleted,
        SubagentEvent,
        SubagentStarted,
    )

    started = SubagentStarted(
        request_id="p",
        parent_call_id="c",
        run_id="r",
        child_thread_id="t",
    )
    completed = SubagentCompleted(
        request_id="p",
        parent_call_id="c",
        run_id="r",
        child_thread_id="t",
    )
    live = SubagentEvent(
        request_id="p",
        parent_call_id="c",
        run_id="r",
        child_thread_id="t",
        inner=AssistantDelta(request_id="child", delta="x"),
    )
    assert is_durable_event(started)
    assert is_durable_event(completed)
    assert not is_durable_event(live)
    assert "SubagentStarted" in DURABLE_EVENT_KINDS
    assert "SubagentCompleted" in DURABLE_EVENT_KINDS
    assert "SubagentEvent" not in DURABLE_EVENT_KINDS


def test_assistant_text_ended_roundtrip_with_text() -> None:
    ev = AssistantTextEnded(request_id="r1", text="hello world")
    assert is_durable_event(ev)
    out = event_from_json(event_to_json(ev))
    assert out == ev
    payload = json.loads(event_to_json(ev))
    assert payload["text"] == "hello world"


@pytest.mark.asyncio
async def test_transcript_skips_live_by_default(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess", workspace_root=tmp_path)
    await writer.ensure_manifest()
    await writer.write_event(AssistantDelta(request_id="r", delta="x"))
    await writer.write_event(
        ToolCallResult(request_id="r", tool="read_file", result="ok", call_id="c1")
    )
    lines = [
        json.loads(line)
        for line in writer.path.read_text(encoding="utf-8").splitlines()
    ]
    types = [line["type"] for line in lines]
    assert types[0] == "SessionManifest"
    assert "durable_only" not in lines[0]
    assert "AssistantDelta" not in types
    assert "ToolCallResult" in types
