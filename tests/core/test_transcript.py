"""Unit tests for monkeybot.core.persistence.transcript."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.path_safety import sanitize_path_component
from monkeybot.core.persistence.transcript import TranscriptWriter, transcript_enabled_from_env
from monkeybot.core.runtime.events import AssistantDelta, ToolCallStarted, TurnComplete, UsageTotals


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_transcript_enabled_from_env_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_TRANSCRIPT_ENABLED", raising=False)
    assert transcript_enabled_from_env() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_transcript_enabled_from_env_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MONKEYBOT_TRANSCRIPT_ENABLED", value)
    assert transcript_enabled_from_env() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_transcript_enabled_from_env_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MONKEYBOT_TRANSCRIPT_ENABLED", value)
    assert transcript_enabled_from_env() is False


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
    await writer.write_event(AssistantDelta(request_id="r1", delta="hi"))
    await writer.write_event(
        ToolCallStarted(request_id="r1", tool="read_file", label="Read x", args={"path": "x"})
    )
    await writer.write_event(TurnComplete(request_id="r1", usage=UsageTotals(input_tokens=5)))

    lines = _read_lines(writer.path)
    assert lines[0]["type"] == "AssistantDelta"
    assert lines[0]["delta"] == "hi"
    assert lines[1]["type"] == "ToolCallStarted"
    assert lines[1]["tool"] == "read_file"
    assert lines[1]["args"] == {"path": "x"}
    assert lines[2]["type"] == "TurnComplete"
    assert lines[2]["usage"]["input_tokens"] == 5


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
        messages=[{"role": "assistant", "content": "tool call"}, {"role": "user", "content": "tool result"}],
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
    await writer.write_event(AssistantDelta(request_id="r1", delta="hey"))

    lines = _read_lines(writer.path)
    assert "seq" not in lines[0]
    assert lines[1]["seq"] == 1
    assert lines[2]["seq"] == 2


@pytest.mark.asyncio
async def test_file_created_under_dot_monkeybot_transcripts(tmp_path: Path) -> None:
    writer = TranscriptWriter("abc-123", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")

    expected = tmp_path / ".monkeybot" / "transcripts" / "abc-123.ndjson"
    assert writer.path == expected
    assert expected.is_file()


@pytest.mark.asyncio
async def test_transcript_path_sanitizes_session_id(tmp_path: Path) -> None:
    """Path traversal in session_id must not escape the transcripts directory."""
    malicious = "../../../../tmp/pwned"
    writer = TranscriptWriter(malicious, workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-5")

    expected = tmp_path / ".monkeybot" / "transcripts" / f"{sanitize_path_component(malicious)}.ndjson"
    assert writer.path == expected
    assert expected.is_file()
    assert not (tmp_path.parent.parent / "tmp" / "pwned.ndjson").exists()
