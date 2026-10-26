"""T2-OPS-04..06: sqlite span tree, subagent traceparent, and transcript records."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.subagents.subagent_worker import (
    _attach_trace_from_envelope,
    _detach_trace,
)
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.observability import init_observability, shutdown_observability
from monkeybot.observability import _state as otel_state
from monkeybot.observability.spans import is_denied_attribute_key, span_subagent
from testkit.context import _ctx, _ctx_with_task
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.mcp import NoMCP
from testkit.provider import FakeProvider

_SECRET = "sk-live-OPS04-SECRET"
_LONG = "A" * 8000 + _SECRET
_DELTA_TYPES = frozenset(
    {"AssistantDelta", "ToolInputDelta", "ThinkingDelta", "ThinkingBlockDelta"}
)
_REPO = Path(__file__).resolve().parents[3]
_FAKE_WORKER = _REPO / "testkit" / "subagent_worker.py"


def _reset_otel() -> None:
    from opentelemetry import trace

    shutdown_observability()
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    otel_state._initialized = False
    otel_state._enabled = False


@pytest.fixture
def otel_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "traces.sqlite"
    _reset_otel()
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "sqlite")
    monkeypatch.setenv("MONKEYBOT_TRACES_DB", str(db))
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    assert init_observability() is True
    try:
        yield db
    finally:
        _reset_otel()


def _spans(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM spans ORDER BY start_time_ns"))
    finally:
        conn.close()


def _trace_id(db: Path, name: str) -> str:
    rows = [row for row in _spans(db) if row["name"] == name]
    assert rows, f"missing span {name}"
    return str(rows[0]["trace_id"])


@pytest.mark.case("T2-OPS-04")
async def test_otel_span_tree_and_secret_redaction(otel_sqlite: Path) -> None:
    """Scripted tool turn: run → turn → llm and tool; trace id matches; secrets truncated or denylisted."""
    ctx = _ctx()
    history = FakeHistory()
    provider = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="read_file", args={"path": "a.txt", "blob": _LONG}),
                Done(),
            ],
            [TextDelta(text="finished"), Done()],
        ]
    )
    events: list[object] = []
    async for event in run(
        "read the file",
        ctx,
        provider=provider,
        history=history,
        inspectors=[],
        tool_executor=RecordingExecutor((_LONG, None)),
        hook_manager=None,
    ):
        events.append(event)
    shutdown_observability()
    rows = _spans(otel_sqlite)
    by_id = {str(row["span_id"]): row for row in rows}
    run_rows = [row for row in rows if row["name"] == "monkeybot.run"]
    turn_rows = [row for row in rows if row["name"] == "monkeybot.turn"]
    llm_rows = [row for row in rows if row["name"] == "monkeybot.llm.stream"]
    tool_rows = [row for row in rows if row["name"] == "monkeybot.tool"]
    assert len(run_rows) == 1
    assert turn_rows
    assert llm_rows
    assert tool_rows
    run_span = run_rows[0]
    assert not run_span["parent_span_id"]
    root_trace = str(run_span["trace_id"])
    for turn in turn_rows:
        assert turn["parent_span_id"] == run_span["span_id"]
        assert str(turn["trace_id"]) == root_trace
    for child in (*llm_rows, *tool_rows):
        parent = by_id[str(child["parent_span_id"])]
        assert parent["name"] == "monkeybot.turn"
        assert str(child["trace_id"]) == root_trace
    completes = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completes) == 1
    assert completes[0].trace_id == root_trace
    tool_attributes = json.loads(tool_rows[0]["attributes_json"] or "{}")
    for key in ("tool.input", "tool.output"):
        value = str(tool_attributes[key])
        assert value.endswith("…[truncated]")
        assert _SECRET not in value
    for row in rows:
        attributes = json.loads(row["attributes_json"] or "{}")
        denied = [key for key in attributes if is_denied_attribute_key(str(key))]
        assert not denied, denied


@pytest.mark.case("T2-OPS-05")
async def test_task_envelope_traceparent_shares_trace_id(
    otel_sqlite: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task envelope carries traceparent, and a child span on that parent shares the trace id."""
    assert _FAKE_WORKER.is_file()
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "AGENT.md").write_text("# agent\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(agent))
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(_FAKE_WORKER))
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'runs.db'}")
    await backend.open()
    try:
        executor = CoreToolExecutor(
            workspace_root=workspace,
            memory=None,
            skills_path=skills,
            mcp=NoMCP(),
            run_store=backend.runs(),
        )
        provider = FakeProvider(
            [
                [ToolCall(call_id="c1", name="task", args={"task": "echo"}), Done()],
                [TextDelta(text="done"), Done()],
            ]
        )
        ctx = _ctx_with_task(workspace_root=workspace)
        async for _event in run(
            "run the child",
            ctx,
            provider=provider,
            history=FakeHistory(),
            inspectors=[],
            tool_executor=executor,
            hook_manager=None,
        ):
            pass
        cursor = await backend._conn.execute("SELECT envelope_json FROM subagent_runs")
        stored = await cursor.fetchall()
        await cursor.close()
    finally:
        await backend.close()
    assert stored, "task did not record a subagent envelope"
    envelope = SubagentEnvelope.from_json(str(stored[0][0]))
    assert envelope.traceparent
    root_trace = envelope.traceparent.split("-")[1]
    token = _attach_trace_from_envelope(envelope)
    try:
        async with span_subagent(
            thread_id="child",
            request_id="child-req",
            parent_run_id=envelope.parent_run_id,
            subagent_type=envelope.subagent_type,
            agent_md=envelope.agent_md,
        ):
            pass
    finally:
        _detach_trace(token)
    shutdown_observability()
    assert _trace_id(otel_sqlite, "monkeybot.run") == root_trace
    assert _trace_id(otel_sqlite, "monkeybot.subagent") == root_trace


@pytest.mark.case("T2-OPS-06")
async def test_transcript_has_manifest_and_provider_records_without_deltas(
    tmp_path: Path,
) -> None:
    """Transcript has SessionManifest, provider request and response records, and no deltas."""
    writer = TranscriptWriter("ops-06", workspace_root=tmp_path, provider_records=True)
    await writer.ensure_manifest(model="fake", provider="fake")
    await writer.write_user_message(request_id="r1", content="hello")
    ctx = _ctx(workspace_root=tmp_path)
    async for event in run(
        "hello",
        ctx,
        provider=FakeProvider(
            [
                [ToolCall(call_id="c1", name="read_file", args={"path": "a.txt"}), Done()],
                [TextDelta(text="finished"), Done()],
            ]
        ),
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        hook_manager=None,
        transcript_writer=writer,
    ):
        await writer.write_event(event)
    types = [
        json.loads(line)["type"]
        for line in writer.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "SessionManifest" in types
    assert "ProviderRequest" in types
    assert "ProviderResponse" in types
    assert _DELTA_TYPES.isdisjoint(types)
