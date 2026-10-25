"""Child transcripts land under the parent session when capture is on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.subagents.subagent_worker import _open_child_transcript


def _envelope(*, parent_session_id: str | None) -> SubagentEnvelope:
    return SubagentEnvelope(
        task="find the bug",
        context="",
        memory_storage_uri="",
        parent_run_id="run-1",
        parent_session_id=parent_session_id,
        subagent_type="explore",
        child_thread_id="child-9",
    )


@pytest.mark.asyncio
async def test_open_child_transcript_writes_under_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "monkeybot.core.subagents.subagent_worker.transcript_enabled_from_config",
        lambda _path=None: True,
    )
    writer = await _open_child_transcript(
        _envelope(parent_session_id="parent-1"),
        workspace=tmp_path,
        thread_id="child-9",
        request_id="sub-1",
        user_text="find the bug",
        config_path=None,
        agent_md=tmp_path / "AGENT.md",
        provider_name="fake",
        memory_on=False,
    )
    assert writer is not None
    assert writer.path.parent.name == "child-9"
    assert writer.path.parent.parent.name == "subagents"
    lines = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "SessionManifest"
    assert lines[0]["parent_session_id"] == "parent-1"
    assert lines[0]["subagent_type"] == "explore"
    assert lines[0]["parent_run_id"] == "run-1"
    assert lines[1]["type"] == "UserMessage"
    assert lines[1]["content"] == "find the bug"


@pytest.mark.asyncio
async def test_open_child_transcript_skips_when_disabled_or_unscoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "monkeybot.core.subagents.subagent_worker.transcript_enabled_from_config",
        lambda _path=None: False,
    )
    kwargs = {
        "workspace": tmp_path,
        "thread_id": "child-9",
        "request_id": "sub-1",
        "user_text": "find the bug",
        "config_path": None,
        "agent_md": tmp_path / "AGENT.md",
        "provider_name": "fake",
        "memory_on": False,
    }
    assert await _open_child_transcript(_envelope(parent_session_id="parent-1"), **kwargs) is None

    monkeypatch.setattr(
        "monkeybot.core.subagents.subagent_worker.transcript_enabled_from_config",
        lambda _path=None: True,
    )
    assert await _open_child_transcript(_envelope(parent_session_id=None), **kwargs) is None
