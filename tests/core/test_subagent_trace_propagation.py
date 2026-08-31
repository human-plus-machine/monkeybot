"""Integration tests for subagent W3C trace propagation."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import propagate, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import unwrap_tool_execution_result
from tests.core.memory.helpers import make_memory_subsystem
from monkeybot.observability.propagation import inject_traceparent
from monkeybot.observability.spans import span_subagent, span_tool

_W3C_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[1-9a-f]$")


def _mem_sub(root: Path) -> MemorySubsystem:
    return make_memory_subsystem(root)


class _NoMCP:
    async def connect(self, *_a: object, **_k: object) -> list:
        return []

    async def connect_streamable_http(self, *_a: object, **_k: object) -> list:
        return []

    async def disconnect(self, *_a: object) -> None:
        return None

    async def call_tool(self, *_a: object, **_k: object) -> str:
        return ""

    def all_tools(self) -> list:
        return []

    def catalog_names(self) -> list:
        return []

    def known_server_names(self) -> list:
        return []

    def is_connected(self, *_a: object) -> bool:
        return False

    def split_prefixed_tool(self, *_a: object) -> None:
        return None

    async def connect_from_catalog(self, *_a: object) -> list:
        return []

    async def load_from_config(self, *_a: object, **_kw: object) -> None:
        return None


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="thread-1",
        request_id="req-1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


def _make_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CoreToolExecutor:
    (tmp_path / "AGENT.md").write_text("# test agent\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = tmp_path / "subagent_worker.py"
    worker.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )


@pytest.fixture(autouse=True)
def _w3c_textmap() -> None:
    propagate.set_global_textmap(TraceContextTextMapPropagator())


@pytest.mark.asyncio
async def test_tool_task_stdin_contains_traceparent_when_parent_span_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    captured: dict[str, object] = {}

    async def fake_spawn(
        script: str,
        envelope: SubagentEnvelope,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        captured["stdin_json"] = envelope.to_json()
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)
    ex = _make_executor(tmp_path, monkeypatch)
    ctx = _ctx()

    async with span_tool(
        tool_name="task",
        tool_call_id="c1",
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        args={"task": "do work"},
    ):
        out, err = unwrap_tool_execution_result(await ex.execute(
            call=ToolCall(call_id="c1", name="task", args={"task": "do work", "context": ""}),
            ctx=ctx,
        ))

    assert err is None and out is not None
    raw = captured["stdin_json"]
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert "traceparent" in parsed
    assert _W3C_TRACEPARENT.match(parsed["traceparent"])


@pytest.mark.asyncio
async def test_tool_task_sets_subagent_otel_service_name_in_child_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    captured_env: dict[str, str] = {}

    async def fake_create_subprocess_exec(
        *cmd: str | bytes,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        del cmd, kwargs
        if env is not None:
            captured_env.update(env)
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    async def fake_spawn(
        script: str,
        envelope: SubagentEnvelope,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, on_event
        if subprocess_exec is not None:
            await subprocess_exec(sys.executable, "-c", "pass")
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)
    ex = _make_executor(tmp_path, monkeypatch)
    ctx = _ctx()

    async with span_tool(
        tool_name="task",
        tool_call_id="c1",
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        args={"task": "do work"},
    ):
        out, err = unwrap_tool_execution_result(await ex.execute(
            call=ToolCall(call_id="c1", name="task", args={"task": "do work", "context": ""}),
            ctx=ctx,
        ))

    assert err is None and out is not None
    assert captured_env.get("OTEL_SERVICE_NAME") == "monkeybot-subagent"


@pytest.mark.asyncio
async def test_tool_task_spawns_in_new_session_for_process_group_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline task must start_new_session so timeout/cancel can killpg descendants."""
    captured_kwargs: dict[str, object] = {}

    async def fake_create_subprocess_exec(
        *cmd: str | bytes,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        del cmd, env
        captured_kwargs.update(kwargs)
        proc = MagicMock()
        proc.pid = 4242
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    async def fake_spawn(
        script: str,
        envelope: SubagentEnvelope,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, on_event, envelope
        if subprocess_exec is not None:
            await subprocess_exec(sys.executable, "-c", "pass")
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)
    monkeypatch.setattr(
        "monkeybot.core.subprocess_groups.SUPPORTS_PROCESS_GROUPS",
        True,
    )
    monkeypatch.setattr(
        "monkeybot.core.subprocess_groups.process_group_id",
        lambda pid: pid,
    )
    ex = _make_executor(tmp_path, monkeypatch)
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="c1", name="task", args={"task": "do work", "context": ""}),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    assert captured_kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_tool_task_stdin_omits_traceparent_without_active_span(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    captured: dict[str, object] = {}

    async def fake_spawn(
        script: str,
        envelope: SubagentEnvelope,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        captured["stdin_json"] = envelope.to_json()
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)
    ex = _make_executor(tmp_path, monkeypatch)
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(call_id="c1", name="task", args={"task": "do work", "context": ""}),
        ctx=ctx,
    ))
    assert err is None and out is not None
    parsed = json.loads(str(captured["stdin_json"]))
    assert "traceparent" not in parsed


@pytest.mark.asyncio
async def test_span_subagent_emits_stable_name_when_enabled(otel_memory_exporter) -> None:
    from opentelemetry import context as otel_context

    from monkeybot.observability.propagation import extract_traceparent

    carrier: dict[str, str] = {}
    tracer = trace.get_tracer("parent")
    with tracer.start_as_current_span("parent") as parent:
        inject_traceparent(carrier)
        parent_span_id = parent.get_span_context().span_id

    ctx = extract_traceparent(carrier)
    assert ctx is not None
    token = otel_context.attach(ctx)
    try:
        async with span_subagent(
            thread_id="thread-1",
            request_id="req-1",
            parent_run_id="parent-run",
        ):
            pass
    finally:
        otel_context.detach(token)

    spans = otel_memory_exporter.get_finished_spans()  # type: ignore[attr-defined]
    sub = next(s for s in spans if s.name == "monkeybot.subagent")
    assert sub.attributes.get("thread.id") == "thread-1"
    assert sub.attributes.get("request.id") == "req-1"
    assert sub.attributes.get("parent.run.id") == "parent-run"
    assert sub.parent is not None
    assert format(sub.parent.span_id, "016x") == format(parent_span_id, "016x")


def _parent_traceparent() -> tuple[str, int]:
    carrier: dict[str, str] = {}
    tracer = trace.get_tracer("parent-fixture")
    with tracer.start_as_current_span("parent-fixture") as span:
        inject_traceparent(carrier)
        trace_id = span.get_span_context().trace_id
    tp = carrier.get("traceparent")
    assert tp is not None
    return tp, trace_id


def _install_worker_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    envelope: SubagentEnvelope,
    otel_memory_exporter: object,
) -> None:
    from monkeybot.core.subagents import subagent_worker

    ws = tmp_path / "ws"
    ws.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    mem_uri = envelope.memory_storage_uri

    monkeypatch.setenv("MONKEYBOT_SUBAGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("MEMORY_STORAGE_URI", mem_uri)
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SKILLS_PATH", str(skills))
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv(
        "MONKEYBOT_FAKE_PROVIDER_EVENTS",
        json.dumps([[{"kind": "text_delta", "text": "ok"}, {"kind": "done"}]]),
    )
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope.to_json()))

    backend = MagicMock()
    backend.open = AsyncMock()
    backend.close = AsyncMock()
    backend.history.return_value = MagicMock()
    monkeypatch.setattr(subagent_worker, "create_storage_backend", lambda _url, **_kw: backend)

    mcp = MagicMock()
    mcp.load_from_config = AsyncMock()
    mcp.disconnect = AsyncMock()
    mcp._servers = {}
    monkeypatch.setattr(subagent_worker, "MCPClient", lambda: mcp)

    async def _fake_build_context(*_a: object, **_k: object) -> TurnContext:
        return _ctx()

    monkeypatch.setattr(subagent_worker, "build_context", _fake_build_context)

    async def _fake_run_loop(*_a: object, **_k: object):
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr(subagent_worker, "run_loop", _fake_run_loop)

    def _init_with_memory_exporter() -> bool:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        from monkeybot.observability import init_observability

        provider = trace.get_tracer_provider()
        if hasattr(provider, "add_span_processor"):
            provider.add_span_processor(SimpleSpanProcessor(otel_memory_exporter))  # type: ignore[arg-type]
        return init_observability()

    monkeypatch.setattr(subagent_worker, "init_observability", _init_with_memory_exporter)
    monkeypatch.setattr(subagent_worker, "shutdown_observability", lambda: None)


@pytest.mark.asyncio
async def test_worker_linked_trace_fixture_or_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    from monkeybot.core.subagents import subagent_worker

    traceparent, parent_trace_id = _parent_traceparent()
    envelope = SubagentEnvelope(
        task="linked trace",
        context="",
        memory_storage_uri="local://" + str((tmp_path / "mem").resolve()),
        parent_run_id="parent-1",
        traceparent=traceparent,
    )
    (tmp_path / "mem").mkdir()
    _install_worker_mocks(monkeypatch, tmp_path, envelope=envelope, otel_memory_exporter=otel_memory_exporter)

    await subagent_worker._async_main()

    spans = otel_memory_exporter.get_finished_spans()  # type: ignore[attr-defined]
    sub = next(s for s in spans if s.name == "monkeybot.subagent")
    assert sub.context.trace_id == parent_trace_id
    run_spans = [s for s in spans if s.name == "monkeybot.run"]
    assert run_spans
    assert all(s.context.trace_id == parent_trace_id for s in run_spans)


@pytest.mark.asyncio
async def test_malformed_traceparent_worker_starts_new_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from monkeybot.core.subagents import subagent_worker

    caplog.set_level(logging.WARNING)
    _, parent_trace_id = _parent_traceparent()
    envelope = SubagentEnvelope(
        task="malformed trace",
        context="",
        memory_storage_uri="local://" + str((tmp_path / "mem").resolve()),
        parent_run_id="parent-2",
        traceparent="not-a-valid-traceparent",
    )
    (tmp_path / "mem").mkdir()
    _install_worker_mocks(monkeypatch, tmp_path, envelope=envelope, otel_memory_exporter=otel_memory_exporter)

    await subagent_worker._async_main()

    warnings = [r.message.lower() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("traceparent" in msg or "malformed" in msg or "propagat" in msg for msg in warnings)

    spans = otel_memory_exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert spans
    for span in spans:
        assert span.context.trace_id != parent_trace_id


@pytest.mark.asyncio
async def test_legacy_envelope_without_traceparent_starts_disjoint_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    from monkeybot.core.subagents import subagent_worker

    _, parent_trace_id = _parent_traceparent()
    envelope = SubagentEnvelope(
        task="legacy",
        context="",
        memory_storage_uri="local://" + str((tmp_path / "mem").resolve()),
        parent_run_id="parent-3",
    )
    (tmp_path / "mem").mkdir()
    _install_worker_mocks(monkeypatch, tmp_path, envelope=envelope, otel_memory_exporter=otel_memory_exporter)

    await subagent_worker._async_main()

    spans = otel_memory_exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert spans
    worker_trace_ids = {s.context.trace_id for s in spans}
    assert parent_trace_id not in worker_trace_ids


@pytest.mark.asyncio
async def test_worker_completes_without_memory_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    from monkeybot.core.subagents import subagent_worker

    envelope = SubagentEnvelope(
        task="no memory",
        context="",
        memory_storage_uri="",
        parent_run_id="parent-none",
    )
    ignored_mem = tmp_path / "ignored"
    ignored_mem.mkdir()
    monkeypatch.setenv("MEMORY_STORAGE_URI", "local://" + str(ignored_mem.resolve()))

    ws = tmp_path / "ws"
    ws.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SKILLS_PATH", str(skills))
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv(
        "MONKEYBOT_FAKE_PROVIDER_EVENTS",
        json.dumps([[{"kind": "text_delta", "text": "ok"}, {"kind": "done"}]]),
    )
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope.to_json()))

    backend = MagicMock()
    backend.open = AsyncMock()
    backend.close = AsyncMock()
    backend.history.return_value = MagicMock()
    monkeypatch.setattr(subagent_worker, "create_storage_backend", lambda _url, **_kw: backend)

    mcp = MagicMock()
    mcp.load_from_config = AsyncMock()
    mcp.disconnect = AsyncMock()
    mcp._servers = {}
    monkeypatch.setattr(subagent_worker, "MCPClient", lambda: mcp)

    seen_memory: list[object | None] = []

    async def _fake_build_context(*_a: object, memory=None, **_k: object) -> TurnContext:
        seen_memory.append(memory)
        return _ctx()

    monkeypatch.setattr(subagent_worker, "build_context", _fake_build_context)

    async def _fake_run_loop(*_a: object, **_k: object):
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr(subagent_worker, "run_loop", _fake_run_loop)

    def _init_with_memory_exporter() -> bool:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        from monkeybot.observability import init_observability

        provider = trace.get_tracer_provider()
        if hasattr(provider, "add_span_processor"):
            provider.add_span_processor(SimpleSpanProcessor(otel_memory_exporter))  # type: ignore[arg-type]
        return init_observability()

    monkeypatch.setattr(subagent_worker, "init_observability", _init_with_memory_exporter)
    monkeypatch.setattr(subagent_worker, "shutdown_observability", lambda: None)

    await subagent_worker._async_main()

    assert seen_memory == [None]
