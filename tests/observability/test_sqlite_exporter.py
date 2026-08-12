"""Tests for the SQLite span exporter."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from monkeybot.observability.sqlite_exporter import (
    SqliteExporterConfig,
    SqliteSpanExporter,
    load_sqlite_exporter_config,
)


def _reset_global_tracer_provider() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_tracer_provider() -> None:
    _reset_global_tracer_provider()
    yield
    _reset_global_tracer_provider()


def _config(db_path: Path, **kwargs: Any) -> SqliteExporterConfig:
    return SqliteExporterConfig(db_path=db_path, **kwargs)


def _provider(exporter: SqliteSpanExporter) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "test-svc"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def _emit_nested_run(db_path: Path) -> tuple[str, str, str]:
    exporter = SqliteSpanExporter(_config(db_path, workspace_id="ws-1"))
    _provider(exporter)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("monkeybot.run") as run_span:
        run_span.set_attribute("openinference.span.kind", "AGENT")
        run_span.set_attribute("thread.id", "thread-1")
        run_span.set_attribute("request.id", "req-1")
        run_span.set_attribute("agent.name", "my-agent")
        run_span.set_attribute("user.message", "hello user")
        run_span.set_attribute("output.value", "run out")
        with tracer.start_as_current_span("monkeybot.tool") as tool_span:
            tool_span.set_attribute("openinference.span.kind", "TOOL")
            tool_span.set_attribute("thread.id", "thread-1")
            tool_span.set_attribute("request.id", "req-1")
            tool_span.set_attribute("tool.name", "bash")
            tool_span.set_attribute("tool.input", '{"cmd":"ls"}')
            tool_span.set_attribute("tool.output", "ok")
            tool_span.add_event("monkeybot.hook.pre_tool", {"tool.name": "bash"})
    trace_id = format(run_span.context.trace_id, "032x")  # type: ignore[name-defined]
    run_id = format(run_span.context.span_id, "016x")  # type: ignore[name-defined]
    tool_id = format(tool_span.context.span_id, "016x")  # type: ignore[name-defined]
    exporter.shutdown()
    return trace_id, run_id, tool_id


def test_schema_created_with_version(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    tracer = trace.get_tracer("t")
    _provider(exporter)
    with tracer.start_as_current_span("x"):
        pass
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    version = conn.execute("select value from schema_meta where key = 'schema_version'").fetchone()
    assert version == ("1",)
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    assert {"schema_meta", "traces", "spans"} <= tables
    conn.close()


def test_span_row_fields_and_linkage(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    trace_id, run_id, tool_id = _emit_nested_run(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute("select * from spans where span_id = ?", (run_id,)).fetchone()
    tool_row = conn.execute("select * from spans where span_id = ?", (tool_id,)).fetchone()
    assert run_row is not None
    assert tool_row is not None

    assert run_row["trace_id"] == trace_id
    assert run_row["parent_span_id"] is None
    assert run_row["kind"] == "AGENT"
    assert run_row["status_code"] == "UNSET"
    assert run_row["duration_ms"] >= 0
    assert run_row["service_name"] == "test-svc"
    assert run_row["workspace_id"] == "ws-1"
    assert run_row["agent_name"] == "my-agent"
    assert run_row["input_value"] == "hello user"
    assert run_row["output_value"] == "run out"

    assert tool_row["parent_span_id"] == run_id
    assert tool_row["kind"] == "TOOL"
    assert tool_row["tool_name"] == "bash"
    assert tool_row["input_value"] == '{"cmd":"ls"}'
    assert tool_row["output_value"] == "ok"

    events = json.loads(tool_row["events_json"])
    assert events == [
        {
            "name": "monkeybot.hook.pre_tool",
            "at": events[0]["at"],
            "attributes": {"tool.name": "bash"},
        }
    ]
    assert isinstance(events[0]["at"], int)

    attrs = json.loads(tool_row["attributes_json"])
    assert attrs["tool.name"] == "bash"
    conn.close()


def test_input_output_fallback_precedence(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    _provider(exporter)
    tracer = trace.get_tracer("t")

    with tracer.start_as_current_span("monkeybot.llm.stream") as span:
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("input.value", "preferred-in")
        span.set_attribute("trace.input", "trace-in")
        span.set_attribute("gen_ai.prompt", "prompt-in")
        span.set_attribute("output.value", "preferred-out")
        span.set_attribute("gen_ai.completion", "completion-out")
        span.set_attribute("gen_ai.request.model", "gpt-test")
        span.set_attribute("gen_ai.usage.input_tokens", 11)
        span.set_attribute("gen_ai.usage.output_tokens", 22)
        span.set_attribute("gen_ai.usage.total_tokens", 33)

    exporter.shutdown()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("select * from spans limit 1").fetchone()
    assert row is not None
    assert row["input_value"] == "preferred-in"
    assert row["output_value"] == "preferred-out"
    assert row["model"] == "gpt-test"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 22
    assert row["total_tokens"] == 33
    conn.close()


def test_agent_name_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "traces.db"
    monkeypatch.setenv("AGENT_NAME", "env-agent")
    exporter = SqliteSpanExporter(_config(db_path, agent_name_fallback="env-agent"))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("monkeybot.turn") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    agent_name = conn.execute("select agent_name from spans limit 1").fetchone()
    assert agent_name == ("env-agent",)
    conn.close()


def test_traces_rollup(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    trace_id, run_id, tool_id = _emit_nested_run(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    trace_row = conn.execute("select * from traces where trace_id = ?", (trace_id,)).fetchone()
    assert trace_row is not None
    assert trace_row["span_count"] == 2
    assert trace_row["error_count"] == 0
    assert trace_row["root_span_id"] == run_id
    assert trace_row["root_name"] == "monkeybot.run"
    assert trace_row["thread_id"] == "thread-1"
    assert trace_row["request_id"] == "req-1"
    assert trace_row["agent_name"] == "my-agent"
    assert trace_row["workspace_id"] == "ws-1"
    assert trace_row["input_value"] == "hello user"
    assert trace_row["output_value"] == "run out"
    assert (
        trace_row["start_time_ns"]
        <= conn.execute(
            "select min(start_time_ns) from spans where trace_id = ?", (trace_id,)
        ).fetchone()[0]
    )
    assert (
        trace_row["end_time_ns"]
        >= conn.execute(
            "select max(end_time_ns) from spans where trace_id = ?", (trace_id,)
        ).fetchone()[0]
    )
    assert trace_row["duration_ms"] == pytest.approx(
        (trace_row["end_time_ns"] - trace_row["start_time_ns"]) / 1_000_000
    )
    del run_id, tool_id
    conn.close()


def test_monkeybot_run_wins_root_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    _provider(exporter)
    tracer = trace.get_tracer("t")

    with tracer.start_as_current_span("other-root") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", "other-in")
        with tracer.start_as_current_span("monkeybot.run") as run:
            run.set_attribute("openinference.span.kind", "AGENT")
            run.set_attribute("input.value", "run-in")

    trace_id = format(root.context.trace_id, "032x")
    run_id = format(run.context.span_id, "016x")
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    trace_row = conn.execute("select * from traces where trace_id = ?", (trace_id,)).fetchone()
    assert trace_row is not None
    assert trace_row["root_span_id"] == run_id
    assert trace_row["root_name"] == "monkeybot.run"
    assert trace_row["input_value"] == "run-in"
    conn.close()


def test_error_status_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("monkeybot.tool") as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_status(Status(StatusCode.ERROR, "boom"))
    trace_id = format(span.context.trace_id, "032x")
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    status = conn.execute("select status_code from spans limit 1").fetchone()
    error_count = conn.execute(
        "select error_count from traces where trace_id = ?", (trace_id,)
    ).fetchone()
    assert status == ("ERROR",)
    assert error_count == (1,)
    conn.close()


def test_total_tokens_rollup(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("a") as a:
        a.set_attribute("gen_ai.usage.total_tokens", 10)
        with tracer.start_as_current_span("b") as b:
            b.set_attribute("gen_ai.usage.total_tokens", 5)
        with tracer.start_as_current_span("c"):
            pass
    trace_id = format(a.context.trace_id, "032x")
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    total = conn.execute(
        "select total_tokens from traces where trace_id = ?", (trace_id,)
    ).fetchone()
    assert total == (15,)
    conn.close()


def test_idempotent_reexport(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    sqlite = SqliteSpanExporter(_config(db_path))
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("once") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", "v1")
    finished = list(mem.get_finished_spans())
    span_id = format(finished[0].context.span_id, "016x")
    trace_id = format(finished[0].context.trace_id, "032x")

    sqlite.export(finished)
    finished[0]._attributes["input.value"] = "v2"  # type: ignore[attr-defined]
    sqlite.export(finished)
    sqlite.shutdown()

    conn = sqlite3.connect(db_path)
    input_value = conn.execute(
        "select input_value from spans where span_id = ?", (span_id,)
    ).fetchone()
    span_rows = conn.execute("select count(*) from spans").fetchone()
    span_count = conn.execute(
        "select span_count from traces where trace_id = ?", (trace_id,)
    ).fetchone()
    conn.close()
    assert input_value == ("v2",)
    assert span_rows == (1,)
    assert span_count == (1,)


def test_export_failure_on_unwritable_db(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not-a-directory", encoding="utf-8")
    db_path = blocker / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    trace.set_tracer_provider(provider)
    with trace.get_tracer("t").start_as_current_span("x"):
        pass
    finished = mem.get_finished_spans()
    with caplog.at_level("WARNING"):
        assert exporter.export(finished) is SpanExportResult.FAILURE
        assert exporter.export(finished) is SpanExportResult.FAILURE
    warnings = [r for r in caplog.records if "sqlite span export failed" in r.getMessage()]
    assert len(warnings) == 1
    assert exporter._disabled is True


def test_concurrent_same_trace_insert_no_integrity_error(tmp_path: Path) -> None:
    """Two exporters racing the first insert for one trace_id must both succeed."""
    import threading

    db_path = tmp_path / "traces.db"
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("root") as root:
        root.set_attribute("openinference.span.kind", "AGENT")
        with tracer.start_as_current_span("child") as child:
            child.set_attribute("openinference.span.kind", "CHAIN")
    finished = list(mem.get_finished_spans())
    assert len(finished) == 2

    barrier = threading.Barrier(2)
    results: list[SpanExportResult] = []

    def _export_one(span: object) -> None:
        exporter = SqliteSpanExporter(_config(db_path))
        barrier.wait(timeout=5)
        results.append(exporter.export([span]))  # type: ignore[list-item]
        exporter.shutdown()

    threads = [
        threading.Thread(target=_export_one, args=(finished[0],)),
        threading.Thread(target=_export_one, args=(finished[1],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results == [SpanExportResult.SUCCESS, SpanExportResult.SUCCESS]
    conn = sqlite3.connect(db_path)
    assert conn.execute("select count(*) from spans").fetchone() == (2,)
    trace_id = format(finished[0].context.trace_id, "032x")
    row = conn.execute(
        "select span_count from traces where trace_id = ?", (trace_id,)
    ).fetchone()
    conn.close()
    assert row == (2,)


def test_two_connections_same_file(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter_a = SqliteSpanExporter(_config(db_path, workspace_id="a"))
    exporter_b = SqliteSpanExporter(_config(db_path, workspace_id="b"))

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem_a = InMemorySpanExporter()
    mem_b = InMemorySpanExporter()
    provider_a = TracerProvider()
    provider_b = TracerProvider()
    provider_a.add_span_processor(SimpleSpanProcessor(mem_a))
    provider_b.add_span_processor(SimpleSpanProcessor(mem_b))
    trace.set_tracer_provider(provider_a)
    with trace.get_tracer("a").start_as_current_span("from-a") as sa:
        sa.set_attribute("openinference.span.kind", "AGENT")
    trace.set_tracer_provider(provider_b)
    with trace.get_tracer("b").start_as_current_span("from-b") as sb:
        sb.set_attribute("openinference.span.kind", "AGENT")

    assert exporter_a.export(mem_a.get_finished_spans()) is SpanExportResult.SUCCESS
    assert exporter_b.export(mem_b.get_finished_spans()) is SpanExportResult.SUCCESS
    exporter_a.shutdown()
    exporter_b.shutdown()

    conn = sqlite3.connect(db_path)
    names = {row[0] for row in conn.execute("select name from spans")}
    assert names == {"from-a", "from-b"}
    conn.close()


def test_retention_by_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path, retention_days=1, max_spans=0))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("old"):
        pass
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    old_ns = int((time.time() - 3 * 86400) * 1_000_000_000)
    conn.execute("update spans set start_time_ns = ?, end_time_ns = ?", (old_ns, old_ns + 1))
    conn.execute(
        "update traces set start_time_ns = ?, end_time_ns = ?, updated_at = ?",
        (old_ns, old_ns + 1, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()

    exporter2 = SqliteSpanExporter(_config(db_path, retention_days=1, max_spans=0))
    exporter2._last_retention_at = 0.0
    exporter2._ensure_connection()
    exporter2._run_retention_locked(exporter2._conn)  # type: ignore[arg-type]
    exporter2.shutdown()

    conn = sqlite3.connect(db_path)
    assert conn.execute("select count(*) from spans").fetchone() == (0,)
    assert conn.execute("select count(*) from traces").fetchone() == (0,)
    conn.close()


def test_max_spans_pruning(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path, retention_days=0, max_spans=2))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    for idx in range(3):
        with tracer.start_as_current_span(f"trace-{idx}") as span:
            span.set_attribute("openinference.span.kind", "CHAIN")
            time.sleep(0.001)
    exporter._last_retention_at = 0.0
    exporter._maybe_run_retention(force=True)
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    span_count = conn.execute("select count(*) from spans").fetchone()[0]
    trace_count = conn.execute("select count(*) from traces").fetchone()[0]
    conn.close()
    assert span_count <= 2
    assert trace_count <= 2


def test_force_flush_checkpoints_without_pruning(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path, retention_days=1, max_spans=1))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("keep-me") as span:
        span.set_attribute("openinference.span.kind", "AGENT")
    with tracer.start_as_current_span("also-keep") as span:
        span.set_attribute("openinference.span.kind", "AGENT")

    conn = sqlite3.connect(db_path)
    old_ns = int((time.time() - 3 * 86400) * 1_000_000_000)
    conn.execute("update spans set start_time_ns = ?, end_time_ns = ?", (old_ns, old_ns + 1))
    conn.commit()
    conn.close()

    assert exporter.force_flush() is True
    conn = sqlite3.connect(db_path)
    assert conn.execute("select count(*) from spans").fetchone() == (2,)
    conn.close()
    exporter.shutdown()
    assert db_path.exists()


def test_partial_prune_recomputes_trace_rollups(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path, retention_days=0, max_spans=2))
    _provider(exporter)
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("root") as root:
        root.set_attribute("openinference.span.kind", "AGENT")
        root.set_attribute("gen_ai.usage.total_tokens", 10)
        for idx in range(3):
            with tracer.start_as_current_span(f"child-{idx}") as child:
                child.set_attribute("openinference.span.kind", "CHAIN")
                child.set_attribute("gen_ai.usage.total_tokens", 1)
                time.sleep(0.001)
    trace_id = format(root.context.trace_id, "032x")
    exporter._last_retention_at = 0.0
    exporter._maybe_run_retention(force=True)
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("select count(*) from spans").fetchone()[0] == 2
    trace_row = conn.execute("select * from traces where trace_id = ?", (trace_id,)).fetchone()
    assert trace_row is not None
    assert trace_row["span_count"] == 2
    live_errors = conn.execute(
        "select count(*) from spans where trace_id = ? and status_code = 'ERROR'",
        (trace_id,),
    ).fetchone()[0]
    assert trace_row["error_count"] == live_errors
    live_tokens = conn.execute(
        "select coalesce(sum(total_tokens), 0) from spans where trace_id = ?",
        (trace_id,),
    ).fetchone()[0]
    assert trace_row["total_tokens"] == live_tokens
    conn.close()


def test_schema_version_mismatch_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    trace.set_tracer_provider(provider)
    with trace.get_tracer("t").start_as_current_span("x"):
        pass
    finished = list(mem.get_finished_spans())
    assert exporter.export(finished) is SpanExportResult.SUCCESS
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    conn.execute("update schema_meta set value = '999' where key = 'schema_version'")
    conn.commit()
    conn.close()

    exporter2 = SqliteSpanExporter(_config(db_path))
    assert exporter2.export(finished) is SpanExportResult.FAILURE


def test_denied_keys_dropped_and_values_clipped(tmp_path: Path) -> None:
    """Attributes and events that bypass ``set_span_attribute_safe`` are still sanitized."""
    db_path = tmp_path / "traces.db"
    exporter = SqliteSpanExporter(_config(db_path))
    _provider(exporter)
    tracer = trace.get_tracer("test")
    huge = "x" * 20_000
    allowlisted = "y" * 6_000
    with tracer.start_as_current_span("monkeybot.run") as span:
        # Shapes emitted by FastAPI instrumentation / record_exception, not our helpers.
        span.set_attribute("http.request.header.authorization", "Bearer super-secret")
        span.set_attribute("db.password", "hunter2")
        span.set_attribute("gen_ai.usage.total_tokens", 42)
        span.set_attribute("http.url", huge)
        span.set_attribute("input.value", allowlisted)
        span.set_attribute("gen_ai.prompt", allowlisted)
        span.add_event("exception", {"exception.stacktrace": huge, "api_key": "leaked"})
    exporter.shutdown()

    conn = sqlite3.connect(db_path)
    attrs_json, events_json, total, input_value = conn.execute(
        "select attributes_json, events_json, total_tokens, input_value from spans"
    ).fetchone()
    conn.close()

    attrs = json.loads(attrs_json)
    assert "http.request.header.authorization" not in attrs
    assert "db.password" not in attrs
    # gen_ai.* keys are exempt from the denylist so token counts survive.
    assert total == 42
    assert len(attrs["http.url"]) < 5_000
    assert attrs["http.url"].endswith("…[truncated]")
    # Allowlisted I/O keys are not clipped to the 4 KiB persist limit.
    assert attrs["input.value"] == allowlisted
    assert attrs["gen_ai.prompt"] == allowlisted
    assert input_value == allowlisted

    events = json.loads(events_json)
    assert "api_key" not in events[0]["attributes"]
    assert len(events[0]["attributes"]["exception.stacktrace"]) < 5_000


def test_load_sqlite_exporter_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MONKEYBOT_TRACES_DB", raising=False)
    assert load_sqlite_exporter_config() is None

    db = tmp_path / "t.db"
    monkeypatch.setenv("MONKEYBOT_TRACES_DB", str(db))
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ID", "ws")
    monkeypatch.setenv("AGENT_NAME", "agent")
    monkeypatch.setenv("MONKEYBOT_TRACES_RETENTION_DAYS", "3")
    monkeypatch.setenv("MONKEYBOT_TRACES_MAX_SPANS", "99")
    cfg = load_sqlite_exporter_config()
    assert cfg is not None
    assert cfg.db_path == db
    assert cfg.workspace_id == "ws"
    assert cfg.agent_name_fallback == "agent"
    assert cfg.retention_days == 3
    assert cfg.max_spans == 99
