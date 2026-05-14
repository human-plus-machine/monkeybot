from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
from pathlib import Path

import pytest

from monkeybot.core.events import (
    ErrorEvent,
    SubagentCompleted,
    SubagentStarted,
    TurnComplete,
)
from monkeybot.core.runs import cleanup_old_runs, create_scratch_dir
from monkeybot.core.subagent_proto import (
    SubagentEnvelope,
    emit_event,
    read_envelope_from_stdin,
    spawn_subagent,
)

_ECHO_AGENT = str(Path(__file__).parent.parent / "fixtures" / "echo_agent.py")

_SAMPLE_ENVELOPE = SubagentEnvelope(
    run_id="01TESTRUN",
    parent_run_id=None,
    agent_name="test-agent",
    task="do something",
    context={"key": "value"},
    skills_path="/skills",
    model="test-model",
    scratch_dir="/tmp/scratch",
)


def test_envelope_json_roundtrip() -> None:
    """SubagentEnvelope survives a dataclasses.asdict → SubagentEnvelope(**data) roundtrip."""
    d = dataclasses.asdict(_SAMPLE_ENVELOPE)
    restored = SubagentEnvelope(**d)
    assert restored == _SAMPLE_ENVELOPE


async def test_spawn_echo_script() -> None:
    """Spawning echo_agent.py yields SubagentStarted → TurnComplete → SubagentCompleted."""
    events = []
    async for ev in spawn_subagent(_ECHO_AGENT, "hello"):
        events.append(ev)

    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "SubagentStarted"
    assert "TurnComplete" in kinds
    assert kinds[-1] == "SubagentCompleted"

    started = events[0]
    assert isinstance(started, SubagentStarted)
    assert started.script == _ECHO_AGENT

    completed = events[-1]
    assert isinstance(completed, SubagentCompleted)
    assert completed.run_id == started.run_id


async def test_spawn_malformed_line(tmp_path: Path) -> None:
    """A script that emits garbage yields a recoverable ErrorEvent."""
    script = tmp_path / "malformed.py"
    script.write_text("import sys\nsys.path.insert(0,'src')\nprint('garbage')\n")
    events = []
    async for ev in spawn_subagent(str(script), "test"):
        events.append(ev)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert any(e.recoverable for e in error_events)


async def test_spawn_timeout(tmp_path: Path) -> None:
    """A script that never exits yields an ErrorEvent containing 'timeout'."""
    script = tmp_path / "sleepy.py"
    script.write_text("import time\ntime.sleep(999)\n")
    events = []
    async for ev in spawn_subagent(str(script), "test", timeout_seconds=1):
        events.append(ev)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert any("timeout" in e.message.lower() for e in error_events)


async def test_spawn_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit without TurnComplete yields ErrorEvent then SubagentCompleted."""
    script = tmp_path / "bail.py"
    script.write_text("import sys\nsys.exit(1)\n")
    events = []
    async for ev in spawn_subagent(str(script), "test"):
        events.append(ev)

    assert isinstance(events[-1], SubagentCompleted)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert error_events, "Expected at least one ErrorEvent for non-zero exit"


def test_emit_event_writes_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """emit_event writes a valid JSON line to stdout and flushes."""
    buf = io.StringIO()
    monkeypatch.setattr("monkeybot.core.subagent_proto.sys.stdout", buf)
    emit_event(TurnComplete(run_id="test-run"))
    output = buf.getvalue()
    lines = [ln for ln in output.splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["kind"] == "turn_complete"
    assert parsed["run_id"] == "test-run"


def test_read_envelope_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_envelope_from_stdin deserializes a JSON line from stdin."""
    raw = json.dumps(dataclasses.asdict(_SAMPLE_ENVELOPE)) + "\n"
    monkeypatch.setattr("monkeybot.core.subagent_proto.sys.stdin", io.StringIO(raw))
    envelope = read_envelope_from_stdin()
    assert envelope.run_id == _SAMPLE_ENVELOPE.run_id
    assert envelope.task == _SAMPLE_ENVELOPE.task
    assert envelope.context == _SAMPLE_ENVELOPE.context


def test_create_scratch_dir_mode(tmp_path: Path) -> None:
    """create_scratch_dir creates dir with 0o700 permissions containing run_id."""
    run_id = "testrun123"
    result = create_scratch_dir(run_id, str(tmp_path))
    assert os.path.isdir(result)
    assert run_id in result
    mode = stat.S_IMODE(os.stat(result).st_mode)
    assert mode == 0o700


def test_cleanup_old_runs_returns_count(tmp_path: Path) -> None:
    """cleanup_old_runs deletes dirs older than max_age_days and returns count."""
    for i in range(3):
        d = tmp_path / f"monkeybot-run-{i:03}"
        d.mkdir()

    # Backdate two dirs to make them appear old
    old_time = 0.0  # epoch — definitely older than 7 days
    for name in ["monkeybot-run-000", "monkeybot-run-001"]:
        p = tmp_path / name
        os.utime(p, (old_time, old_time))

    deleted = cleanup_old_runs(str(tmp_path), max_age_days=7)
    assert deleted == 2
    assert (tmp_path / "monkeybot-run-002").exists()
