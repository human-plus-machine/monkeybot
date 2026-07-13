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


def test_assistant_text_ended_roundtrip_with_text() -> None:
    ev = AssistantTextEnded(request_id="r1", text="hello world")
    assert is_durable_event(ev)
    out = event_from_json(event_to_json(ev))
    assert out == ev
    payload = json.loads(event_to_json(ev))
    assert payload["text"] == "hello world"


@pytest.mark.asyncio
async def test_transcript_skips_live_by_default(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess", workspace_root=tmp_path, include_live=False)
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
    assert lines[0]["durable_only"] is True
    assert "AssistantDelta" not in types
    assert "ToolCallResult" in types


@pytest.mark.asyncio
async def test_transcript_include_live(tmp_path: Path) -> None:
    writer = TranscriptWriter("sess", workspace_root=tmp_path, include_live=True)
    await writer.ensure_manifest()
    await writer.write_event(AssistantDelta(request_id="r", delta="x"))
    lines = [
        json.loads(line)
        for line in writer.path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(line.get("type") == "AssistantDelta" for line in lines)
