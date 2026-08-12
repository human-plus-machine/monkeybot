"""SQLite span exporter for local trace persistence (``OTEL_TRACES_EXPORTER=sqlite``).

Persists completed OTel spans to ``MONKEYBOT_TRACES_DB``. Attribute sanitization
re-applies the secret denylist at write time, but ``gen_ai.*`` keys (including
``gen_ai.prompt`` / ``gen_ai.completion``) are intentionally kept so local
trace UIs can show LLM I/O — this is a local file, not a redacted remote sink.
Allowlisted I/O keys are not re-truncated at persist (parity with OTLP).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from monkeybot.observability.spans import (
    is_attr_truncation_exempt,
    is_denied_attribute_key,
    truncate,
)

logger = logging.getLogger(__name__)

# Third-party instrumentation and OTel ``record_exception`` bypass
# ``set_span_attribute_safe``; re-apply denylist + truncation at persist time.
# Allowlisted keys (``is_attr_truncation_exempt``) skip the byte clip so local
# SQLite stores the same prompt/output length as the OTLP exporter path.
_MAX_PERSISTED_VALUE_BYTES = 4096

_SCHEMA_VERSION = "1"
_RETENTION_INTERVAL_SEC = 60.0
_CONNECT_TIMEOUT_SEC = 5.0

_SCHEMA_DDLS: tuple[str, ...] = (
    """create table if not exists schema_meta (
  key text primary key,
  value text not null
)""",
    """create table if not exists traces (
  trace_id text primary key,
  root_span_id text,
  root_name text,
  thread_id text,
  request_id text,
  agent_name text,
  workspace_id text,
  start_time_ns integer not null,
  end_time_ns integer not null,
  duration_ms real not null default 0,
  span_count integer not null default 0,
  error_count integer not null default 0,
  total_tokens integer,
  input_value text,
  output_value text,
  updated_at integer not null
)""",
    "create index if not exists idx_traces_start on traces (start_time_ns desc)",
    "create index if not exists idx_traces_thread on traces (thread_id, start_time_ns desc)",
    "create index if not exists idx_traces_workspace on traces (workspace_id, start_time_ns desc)",
    """create table if not exists spans (
  span_id text primary key,
  trace_id text not null,
  parent_span_id text,
  name text not null,
  kind text not null default 'UNKNOWN',
  start_time_ns integer not null,
  end_time_ns integer not null,
  duration_ms real not null,
  status_code text not null default 'UNSET',
  status_message text,
  service_name text,
  thread_id text,
  request_id text,
  agent_name text,
  workspace_id text,
  parent_run_id text,
  subagent_type text,
  tool_name text,
  model text,
  input_tokens integer,
  output_tokens integer,
  total_tokens integer,
  input_value text,
  output_value text,
  attributes_json text not null default '{}',
  events_json text not null default '[]',
  inserted_at integer not null
)""",
    "create index if not exists idx_spans_trace on spans (trace_id, start_time_ns)",
    "create index if not exists idx_spans_parent on spans (parent_span_id)",
    "create index if not exists idx_spans_start on spans (start_time_ns desc)",
    "create index if not exists idx_spans_name on spans (name)",
    "create index if not exists idx_spans_thread on spans (thread_id)",
)

_INPUT_VALUE_KEYS = ("input.value", "trace.input", "tool.input", "gen_ai.prompt", "user.message")
_OUTPUT_VALUE_KEYS = ("output.value", "trace.output", "tool.output", "gen_ai.completion")


@dataclass(frozen=True)
class SqliteExporterConfig:
    db_path: Path
    retention_days: int = 7
    max_spans: int = 200_000
    workspace_id: str | None = None
    agent_name_fallback: str | None = None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def load_sqlite_exporter_config() -> SqliteExporterConfig | None:
    db_raw = os.environ.get("MONKEYBOT_TRACES_DB", "").strip()
    if not db_raw:
        return None
    workspace_raw = os.environ.get("MONKEYBOT_WORKSPACE_ID", "").strip()
    agent_raw = os.environ.get("AGENT_NAME", "").strip()
    return SqliteExporterConfig(
        db_path=Path(db_raw),
        retention_days=_env_int("MONKEYBOT_TRACES_RETENTION_DAYS", 7),
        max_spans=_env_int("MONKEYBOT_TRACES_MAX_SPANS", 200_000),
        workspace_id=workspace_raw or None,
        agent_name_fallback=agent_raw or None,
    )


def _format_span_id(span_id: int) -> str:
    return format(span_id, "016x")


def _format_trace_id(trace_id: int) -> str:
    return format(trace_id, "032x")


def _parent_span_id(span: ReadableSpan) -> str | None:
    parent = span.parent
    if parent is None:
        return None
    return _format_span_id(parent.span_id)


def _attr_str(attrs: Mapping[str, Any], key: str) -> str | None:
    value = attrs.get(key)
    if value is None:
        return None
    return str(value)


def _attr_int(attrs: Mapping[str, Any], key: str) -> int | None:
    value = attrs.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _first_attr(attrs: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = _attr_str(attrs, key)
        if value is not None:
            return value
    return None


def _coerce_json_value(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, str):
        if key is not None and is_attr_truncation_exempt(key):
            return value
        return truncate(value, max_bytes=_MAX_PERSISTED_VALUE_BYTES)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_json_value(item, key=key) for item in value]
    return truncate(str(value), max_bytes=_MAX_PERSISTED_VALUE_BYTES)


def _sanitize_attributes(attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attrs:
        return {}
    return {
        key: _coerce_json_value(val, key=key)
        for key, val in attrs.items()
        if not is_denied_attribute_key(key)
    }


def _serialize_attributes(attrs: Mapping[str, Any]) -> str:
    if not attrs:
        return "{}"
    return json.dumps(dict(attrs), separators=(",", ":"), sort_keys=True)


def _event_at_ms(timestamp_ns: int) -> int:
    return int(timestamp_ns // 1_000_000)


def _serialize_events(events: Sequence[Any]) -> str:
    if not events:
        return "[]"
    payload = [
        {
            "name": event.name,
            "at": _event_at_ms(event.timestamp),
            "attributes": _sanitize_attributes(event.attributes),
        }
        for event in events
    ]
    return json.dumps(payload, separators=(",", ":"))


def _span_kind(attrs: Mapping[str, Any]) -> str:
    raw = attrs.get("openinference.span.kind")
    if raw is None:
        return "UNKNOWN"
    return str(raw).upper()


def _duration_ms(start_time_ns: int, end_time_ns: int) -> float:
    return (end_time_ns - start_time_ns) / 1_000_000


def _root_claim(
    *,
    span_name: str,
    parent_span_id: str | None,
    span_id: str,
    input_value: str | None,
    output_value: str | None,
    existing_root_name: str | None = None,
) -> dict[str, Any] | None:
    """Return root-claim fields when this span should own (or take) the trace root.

    ``existing_root_name is None`` means "claim if this span is a root candidate"
    (used when building the span row). Passing the current DB root decides whether
    an update should replace it (``monkeybot.run`` always wins).
    """
    is_run = span_name == "monkeybot.run"
    is_orphan_root = parent_span_id is None
    if existing_root_name is None:
        if not (is_run or is_orphan_root):
            return None
    elif is_run:
        pass
    elif not (is_orphan_root and existing_root_name != "monkeybot.run"):
        return None
    return {
        "root_span_id": span_id,
        "root_name": span_name,
        "input_value": input_value,
        "output_value": output_value,
    }


def _span_row(span: ReadableSpan, config: SqliteExporterConfig, now_ms: int) -> dict[str, Any]:
    attrs = _sanitize_attributes(span.attributes)
    parent_id = _parent_span_id(span)
    start_time_ns = span.start_time or 0
    end_time_ns = span.end_time or start_time_ns
    status = span.status
    status_code = status.status_code.name if status is not None else "UNSET"
    status_message = status.description if status is not None else None
    if status_message is not None:
        status_message = truncate(status_message, max_bytes=_MAX_PERSISTED_VALUE_BYTES)

    service_name = None
    if span.resource is not None:
        raw_service = span.resource.attributes.get("service.name")
        if raw_service is not None:
            service_name = str(raw_service)

    input_value = _first_attr(attrs, _INPUT_VALUE_KEYS)
    output_value = _first_attr(attrs, _OUTPUT_VALUE_KEYS)
    span_id = _format_span_id(span.context.span_id)
    claim = _root_claim(
        span_name=span.name,
        parent_span_id=parent_id,
        span_id=span_id,
        input_value=input_value,
        output_value=output_value,
    )

    return {
        "span_id": span_id,
        "trace_id": _format_trace_id(span.context.trace_id),
        "parent_span_id": parent_id,
        "name": span.name,
        "kind": _span_kind(attrs),
        "start_time_ns": start_time_ns,
        "end_time_ns": end_time_ns,
        "duration_ms": _duration_ms(start_time_ns, end_time_ns),
        "status_code": status_code,
        "status_message": status_message,
        "service_name": service_name,
        "thread_id": _attr_str(attrs, "thread.id"),
        "request_id": _attr_str(attrs, "request.id"),
        "agent_name": _attr_str(attrs, "agent.name") or config.agent_name_fallback,
        "workspace_id": config.workspace_id,
        "parent_run_id": _attr_str(attrs, "parent.run.id"),
        "subagent_type": _attr_str(attrs, "subagent.type"),
        "tool_name": _attr_str(attrs, "tool.name"),
        "model": _attr_str(attrs, "gen_ai.request.model"),
        "input_tokens": _attr_int(attrs, "gen_ai.usage.input_tokens"),
        "output_tokens": _attr_int(attrs, "gen_ai.usage.output_tokens"),
        "total_tokens": _attr_int(attrs, "gen_ai.usage.total_tokens"),
        "input_value": input_value,
        "output_value": output_value,
        "attributes_json": _serialize_attributes(attrs),
        "events_json": _serialize_events(span.events),
        "inserted_at": now_ms,
        "is_error": 1 if status_code == "ERROR" else 0,
        "claim": claim,
    }


_UPSERT_SPAN_SQL = """
insert or replace into spans (
  span_id, trace_id, parent_span_id, name, kind, start_time_ns, end_time_ns, duration_ms,
  status_code, status_message, service_name, thread_id, request_id, agent_name, workspace_id,
  parent_run_id, subagent_type, tool_name, model, input_tokens, output_tokens, total_tokens,
  input_value, output_value, attributes_json, events_json, inserted_at
) values (
  :span_id, :trace_id, :parent_span_id, :name, :kind, :start_time_ns, :end_time_ns, :duration_ms,
  :status_code, :status_message, :service_name, :thread_id, :request_id, :agent_name, :workspace_id,
  :parent_run_id, :subagent_type, :tool_name, :model, :input_tokens, :output_tokens, :total_tokens,
  :input_value, :output_value, :attributes_json, :events_json, :inserted_at
)
"""

# span_count/error_count/total_tokens start at 0/null; _UPDATE_TRACE_SQL always applies deltas
# so concurrent first-inserts of the same trace_id cannot IntegrityError.
_INSERT_TRACE_SQL = """
insert into traces (
  trace_id, root_span_id, root_name, thread_id, request_id, agent_name, workspace_id,
  start_time_ns, end_time_ns, duration_ms, span_count, error_count, total_tokens,
  input_value, output_value, updated_at
) values (
  :trace_id, :root_span_id, :root_name, :thread_id, :request_id, :agent_name,
  :workspace_id, :start_time_ns, :end_time_ns, :duration_ms, 0, 0, null,
  :input_value, :output_value, :updated_at
)
on conflict(trace_id) do nothing
"""

_UPDATE_TRACE_SQL = """
update traces set
  span_count = span_count + :span_delta,
  error_count = error_count + :error_delta,
  start_time_ns = min(start_time_ns, :start_time_ns),
  end_time_ns = max(end_time_ns, :end_time_ns),
  duration_ms = (max(end_time_ns, :end_time_ns) - min(start_time_ns, :start_time_ns)) / 1000000.0,
  thread_id = coalesce(thread_id, :thread_id),
  request_id = coalesce(request_id, :request_id),
  agent_name = coalesce(agent_name, :agent_name),
  workspace_id = coalesce(workspace_id, :workspace_id),
  total_tokens = case
    when :token_delta is not null then coalesce(total_tokens, 0) + :token_delta
    else total_tokens
  end,
  root_span_id = :root_span_id,
  root_name = :root_name,
  input_value = :input_value,
  output_value = :output_value,
  updated_at = :updated_at
where trace_id = :trace_id
"""

_DELETE_ORPHAN_TRACES_SQL = (
    "delete from traces where trace_id not in (select distinct trace_id from spans)"
)

_RECOMPUTE_TRACE_ROLLUPS_SQL = """
update traces set
  span_count = (select count(*) from spans where spans.trace_id = traces.trace_id),
  error_count = (
    select count(*) from spans
    where spans.trace_id = traces.trace_id and spans.status_code = 'ERROR'
  ),
  total_tokens = (
    select sum(total_tokens) from spans where spans.trace_id = traces.trace_id
  ),
  start_time_ns = (
    select min(start_time_ns) from spans where spans.trace_id = traces.trace_id
  ),
  end_time_ns = (
    select max(end_time_ns) from spans where spans.trace_id = traces.trace_id
  ),
  duration_ms = (
    select (max(end_time_ns) - min(start_time_ns)) / 1000000.0
    from spans where spans.trace_id = traces.trace_id
  )
where exists (select 1 from spans where spans.trace_id = traces.trace_id)
"""


class SqliteSpanExporter(SpanExporter):
    def __init__(self, config: SqliteExporterConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._schema_ready = False
        self._last_retention_at = 0.0
        self._disabled = False
        self._export_error_logged = False

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        db_path = self._config.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=_CONNECT_TIMEOUT_SEC,
            isolation_level="IMMEDIATE",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode = WAL")
        conn.execute("pragma busy_timeout = 5000")
        conn.execute("pragma synchronous = NORMAL")
        self._conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        for ddl in _SCHEMA_DDLS:
            conn.execute(ddl)
        row = conn.execute(
            "select value from schema_meta where key = 'schema_version'"
        ).fetchone()
        if row is None:
            conn.execute(
                "insert into schema_meta (key, value) values (?, ?)",
                ("schema_version", _SCHEMA_VERSION),
            )
        elif str(row[0]) != _SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported traces DB schema_version={row[0]!r}, expected {_SCHEMA_VERSION!r}"
            )
        conn.commit()
        self._schema_ready = True

    def _upsert_trace(
        self, conn: sqlite3.Connection, row: dict[str, Any], *, is_new_span: bool
    ) -> None:
        claim = row["claim"]
        updated_at = row["inserted_at"]
        conn.execute(
            _INSERT_TRACE_SQL,
            {
                "trace_id": row["trace_id"],
                "root_span_id": claim["root_span_id"] if claim else None,
                "root_name": claim["root_name"] if claim else None,
                "thread_id": row["thread_id"],
                "request_id": row["request_id"],
                "agent_name": row["agent_name"],
                "workspace_id": row["workspace_id"],
                "start_time_ns": row["start_time_ns"],
                "end_time_ns": row["end_time_ns"],
                "duration_ms": row["duration_ms"],
                "input_value": claim["input_value"] if claim else None,
                "output_value": claim["output_value"] if claim else None,
                "updated_at": updated_at,
            },
        )
        existing = conn.execute(
            "select root_span_id, root_name, input_value, output_value "
            "from traces where trace_id = ?",
            (row["trace_id"],),
        ).fetchone()
        assert existing is not None
        replace = _root_claim(
            span_name=row["name"],
            parent_span_id=row["parent_span_id"],
            span_id=row["span_id"],
            input_value=row["input_value"],
            output_value=row["output_value"],
            existing_root_name=existing["root_name"],
        )
        conn.execute(
            _UPDATE_TRACE_SQL,
            {
                "trace_id": row["trace_id"],
                "span_delta": 1 if is_new_span else 0,
                "error_delta": row["is_error"] if is_new_span else 0,
                "start_time_ns": row["start_time_ns"],
                "end_time_ns": row["end_time_ns"],
                "thread_id": row["thread_id"],
                "request_id": row["request_id"],
                "agent_name": row["agent_name"],
                "workspace_id": row["workspace_id"],
                "token_delta": row["total_tokens"] if is_new_span else None,
                "root_span_id": (
                    replace["root_span_id"] if replace else existing["root_span_id"]
                ),
                "root_name": replace["root_name"] if replace else existing["root_name"],
                "input_value": (
                    replace["input_value"] if replace else existing["input_value"]
                ),
                "output_value": (
                    replace["output_value"] if replace else existing["output_value"]
                ),
                "updated_at": updated_at,
            },
        )

    def _export_batch(self, spans: Sequence[ReadableSpan]) -> None:
        if not spans:
            return
        conn = self._ensure_connection()
        self._ensure_schema(conn)
        now_ms = int(time.time() * 1000)
        with conn:
            for span in spans:
                row = _span_row(span, self._config, now_ms)
                existing = conn.execute(
                    "select span_id from spans where span_id = ?", (row["span_id"],)
                ).fetchone()
                conn.execute(
                    _UPSERT_SPAN_SQL,
                    {k: v for k, v in row.items() if k not in {"claim", "is_error"}},
                )
                self._upsert_trace(conn, row, is_new_span=existing is None)

    def _maybe_run_retention(self, *, force: bool = False) -> None:
        if self._config.retention_days <= 0 and self._config.max_spans <= 0:
            return
        try:
            with self._lock:
                now = time.monotonic()
                if not force and (now - self._last_retention_at) < _RETENTION_INTERVAL_SEC:
                    return
                self._last_retention_at = now
                if self._conn is None:
                    return
                self._run_retention_locked(self._conn)
        except Exception as exc:
            logger.warning("sqlite trace retention failed: %s", exc)

    def _run_retention_locked(self, conn: sqlite3.Connection) -> None:
        if self._config.retention_days > 0:
            cutoff_ns = int((time.time() - self._config.retention_days * 86400) * 1_000_000_000)
            with conn:
                conn.execute("delete from spans where start_time_ns < ?", (cutoff_ns,))
                conn.execute(_DELETE_ORPHAN_TRACES_SQL)
                conn.execute(_RECOMPUTE_TRACE_ROLLUPS_SQL)

        if self._config.max_spans > 0:
            with conn:
                count_row = conn.execute("select count(*) from spans").fetchone()
                span_count = int(count_row[0]) if count_row is not None else 0
                overage = span_count - self._config.max_spans
                if overage > 0:
                    conn.execute(
                        "delete from spans where span_id in ("
                        "  select span_id from spans order by start_time_ns asc limit ?"
                        ")",
                        (overage,),
                    )
                    conn.execute(_DELETE_ORPHAN_TRACES_SQL)
                    conn.execute(_RECOMPUTE_TRACE_ROLLUPS_SQL)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        if self._disabled:
            return SpanExportResult.FAILURE
        try:
            with self._lock:
                self._export_batch(spans)
            self._maybe_run_retention()
            return SpanExportResult.SUCCESS
        except Exception as exc:
            self._disabled = True
            if not self._export_error_logged:
                logger.warning("sqlite span export failed: %s", exc)
                self._export_error_logged = True
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        try:
            with self._lock:
                if self._conn is None:
                    return True
                self._conn.execute("pragma wal_checkpoint(PASSIVE)")
            return True
        except Exception as exc:
            logger.warning("sqlite span force_flush failed: %s", exc)
            return False

    def shutdown(self) -> None:
        try:
            with self._lock:
                if self._conn is None:
                    return
                try:
                    self._last_retention_at = 0.0
                    self._run_retention_locked(self._conn)
                except Exception as exc:
                    logger.warning("sqlite trace retention on shutdown failed: %s", exc)
                self._conn.close()
                self._conn = None
        except Exception as exc:
            logger.warning("sqlite span exporter shutdown failed: %s", exc)
