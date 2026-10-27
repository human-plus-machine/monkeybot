"""T3-SUB-01: a recorded child NDJSON tape through the parent parser.

``regression/t3/subagents/child_tape.ndjson`` is the stdout of a fake child worker, taken
from a local run. The fake child builds real ``AgentEvent`` values and prints each one through
the same two calls the real worker uses (``_event_for_ndjson_pipe`` then ``event_to_json``),
so the tape includes the pipe redaction of a large ``SystemPromptSnapshot``. No model runs.

Replay starts a stand-in child that prints the committed bytes, and runs the real
``spawn_subagent`` on it: ``progress.jsonl`` must be those bytes, the parsed events must be the
recorded ones, and ``output.json`` must be the final event. ``--update-golden`` re-records the
tape (refused when ``CI`` is set); without it, a fresh run of the fake child must equal the
committed tape.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from monkeybot.core.runtime.events import (
    AssistantDelta,
    Error,
    SystemPromptSnapshot,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    event_from_json,
    event_to_json,
)
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope, spawn_subagent

_REPO = Path(__file__).resolve().parents[2]
_TAPE = _REPO / "regression" / "t3" / "subagents" / "child_tape.ndjson"

_FAKE_CHILD = '''\
"""Fake child worker: print one recorded run the way subagent_worker does."""

import sys

from monkeybot.core.runtime.events import (
    AssistantDelta,
    SystemPromptSnapshot,
    Thinking,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UsageTotals,
    event_to_json,
)
from monkeybot.core.subagents.subagent_worker import _event_for_ndjson_pipe

sys.stdin.read()
request_id = "child"
events = [
    Thinking(request_id=request_id),
    SystemPromptSnapshot(request_id=request_id, inner_turn=1, text="# Agent\\n" + "x" * 4096),
    ThinkingBlockDelta(request_id=request_id, text="Read the notes first.", signature="sig-1"),
    ThinkingBlockComplete(request_id=request_id, signature="sig-1"),
    ToolCallStarted(
        request_id=request_id,
        tool="read_file",
        label="read_file",
        args={"path": "notes.txt"},
        call_id="c1",
    ),
    ToolCallResult(
        request_id=request_id,
        tool="read_file",
        result='{"ok": true, "content": "ship the beta on Friday"}',
        call_id="c1",
    ),
    AssistantDelta(request_id=request_id, delta="The beta ships "),
    AssistantDelta(request_id=request_id, delta="on Friday."),
    TurnComplete(
        request_id=request_id,
        usage=UsageTotals(input_tokens=210, output_tokens=12, cached_tokens=64),
    ),
]
for event in events:
    print(event_to_json(_event_for_ndjson_pipe(event)), flush=True)
'''

_STAND_IN = '''\
"""Stand-in child: ignore the envelope, print the recorded tape byte for byte."""

import sys
from pathlib import Path

sys.stdin.read()
sys.stdout.buffer.write(Path({tape!r}).read_bytes())
sys.stdout.flush()
'''


def _envelope() -> SubagentEnvelope:
    return SubagentEnvelope(
        task="Summarise notes.txt",
        context="",
        memory_storage_uri="local:///mem",
        parent_run_id="parent-1",
    )


async def _run_parent(script: Path, scratch: Path) -> list[object]:
    return [event async for event in spawn_subagent(str(script), _envelope(), scratch_dir=scratch)]


def _record(tmp_path: Path) -> str:
    """Run the fake child as a process and keep its stdout, as the parent would see it."""
    script = tmp_path / "fake_child.py"
    script.write_text(_FAKE_CHILD, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-u", str(script)],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _stand_in(tmp_path: Path) -> Path:
    script = tmp_path / "stand_in_child.py"
    script.write_text(_STAND_IN.format(tape=str(_TAPE)), encoding="utf-8")
    return script


@pytest.mark.case("T3-SUB-01")
async def test_the_recorded_tape_gives_the_same_progress_bytes_and_final_event(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    events = await _run_parent(_stand_in(tmp_path), scratch)

    tape = _TAPE.read_bytes()
    assert (scratch / "progress.jsonl").read_bytes() == tape
    lines = tape.decode("utf-8").splitlines()
    assert [type(event).__name__ for event in events] == [
        type(event_from_json(line)).__name__ for line in lines
    ]
    assert not [event for event in events if isinstance(event, Error)]

    final = events[-1]
    assert isinstance(final, TurnComplete)
    assert final.usage.input_tokens == 210 and final.usage.output_tokens == 12
    assert (scratch / "output.json").read_text(encoding="utf-8") == lines[-1] + "\n"
    assert event_to_json(final) == lines[-1]


@pytest.mark.case("T3-SUB-01")
async def test_the_recorded_tape_keeps_the_pipe_redaction_and_the_tool_round(
    tmp_path: Path,
) -> None:
    events = await _run_parent(_stand_in(tmp_path), tmp_path / "scratch")

    snapshot = next(event for event in events if isinstance(event, SystemPromptSnapshot))
    header = "# Agent\n"
    assert snapshot.text == f"[omitted {len(header) + 4096} chars]"
    started = next(event for event in events if isinstance(event, ToolCallStarted))
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert (started.tool, started.call_id, started.args) == (
        "read_file",
        "c1",
        {"path": "notes.txt"},
    )
    assert result.call_id == started.call_id
    assert json.loads(result.result) == {"ok": True, "content": "ship the beta on Friday"}
    reply = "".join(event.delta for event in events if isinstance(event, AssistantDelta))
    assert reply == "The beta ships on Friday."


@pytest.mark.case("T3-SUB-01")
async def test_the_recording_matches_a_fresh_run_of_the_fake_child(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    fresh = await asyncio.to_thread(_record, tmp_path)
    if request.config.getoption("update_golden"):
        _TAPE.parent.mkdir(parents=True, exist_ok=True)
        _TAPE.write_text(fresh, encoding="utf-8")
        return
    assert _TAPE.is_file(), "missing child_tape.ndjson; re-run with --update-golden"
    assert _TAPE.read_text(encoding="utf-8") == fresh
