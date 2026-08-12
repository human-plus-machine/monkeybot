"""SQLite span exporter for local trace persistence (``OTEL_TRACES_EXPORTER=sqlite``)."""

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

from monkeybot.observability.spans import is_denied_attribute_key, truncate

logger = logging.getLogger(__name__)

# Third-party instrumentation and OTel ``record_exception`` bypass
# ``set_span_attribute_safe``; re-apply denylist + truncation at persist time.
_MAX_PERSISTED_VALUE_BYTES = 4096

_SCHEMA_VERSION = "1"
_RETENTION_INTERVAL_SEC = 60.0
_LOCK_RETRY_DELAYS_MS = (50, 150, 400)

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
    connect_timeout: float = 5.0


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


def _coerce_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return truncate(value, max_bytes=_MAX_PERSISTED_VALUE_BYTES)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_json_value(item) for item in value]
    return truncate(str(value), max_bytes=_MAX_PERSISTED_VALUE_BYTES)


def _sanitize_attributes(attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attrs:
        return {}
    return {
        key: _coerce_json_value(val)
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


def _claims_root(span_name: str, parent_span_id: str | None) -> bool:
    return span_name == "monkeybot.run" or parent_span_id is None


def _should_replace_root(
    span_name: str, parent_span_id: str | None, existing_root_name: str | None
) -> bool:
    if span_name == "monkeybot.run":
        return True
    return parent_span_id is None and (
        existing_root_name is None or existing_root_name != "monkeybot.run"
    )


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
    can_claim_root = _claims_root(span.name, parent_id)
    span_id = _format_span_id(span.context.span_id)

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
        "claim_root_span_id": span_id if can_claim_root else None,
        "claim_root_name": span.name if can_claim_root else None,
        "claim_input_value": input_value if can_claim_root else None,
        "claim_output_value": output_value if can_claim_root else None,
    }


_UPSERT_SPAN_SQL = """
insert into spans (
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
on conflict(span_id) do update set
  trace_id = excluded.trace_id,
  parent_span_id = excluded.parent_span_id,
  name = excluded.name,
  kind = excluded.kind,
  start_time_ns = excluded.start_time_ns,
  end_time_ns = excluded.end_time_ns,
  duration_ms = excluded.duration_ms,
  status_code = excluded.status_code,
  status_message = excluded.status_message,
  service_name = excluded.service_name,
  thread_id = excluded.thread_id,
  request_id = excluded.request_id,
  agent_name = excluded.agent_name,
  workspace_id = excluded.workspace_id,
  parent_run_id = excluded.parent_run_id,
  subagent_type = excluded.subagent_type,
  tool_name = excluded.tool_name,
  model = excluded.model,
  input_tokens = excluded.input_tokens,
  output_tokens = excluded.output_tokens,
  total_tokens = excluded.total_tokens,
  input_value = excluded.input_value,
  output_value = excluded.output_value,
  attributes_json = excluded.attributes_json,
  events_json = excluded.events_json,
  inserted_at = excluded.inserted_at
"""

_INSERT_TRACE_SQL = """
insert into traces (
  trace_id, root_span_id, root_name, thread_id, request_id, agent_name, workspace_id,
  start_time_ns, end_time_ns, duration_ms, span_count, error_count, total_tokens,
  input_value, output_value, updated_at
) values (
  :trace_id, :root_span_id, :root_name, :thread_id, :request_id, :agent_name,
  :workspace_id, :start_time_ns, :end_time_ns, :duration_ms, 1, :is_error, :total_tokens,
  :input_value, :output_value, :updated_at
)
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


def _is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class SqliteSpanExporter(SpanExporter):
    def __init__(self, config: SqliteExporterConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._schema_ready = False
        self._last_retention_at = 0.0

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        db_path = self._config.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=self._config.connect_timeout,
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
        conn.execute(
            "insert into schema_meta (key, value) values (?, ?) "
            "on conflict(key) do update set value = excluded.value",
            ("schema_version", _SCHEMA_VERSION),
        )
        conn.commit()
        self._schema_ready = True

    def _upsert_trace(
        self, conn: sqlite3.Connection, row: dict[str, Any], *, is_new_span: bool
    ) -> None:
        existing = conn.execute(
            "select root_span_id, root_name, input_value, output_value "
            "from traces where trace_id = ?",
            (row["trace_id"],),
        ).fetchone()
        updated_at = row["inserted_at"]

        if existing is None:
            conn.execute(
                _INSERT_TRACE_SQL,
                {
                    "trace_id": row["trace_id"],
                    "root_span_id": row["claim_root_span_id"],
                    "root_name": row["claim_root_name"],
                    "thread_id": row["thread_id"],
                    "request_id": row["request_id"],
                    "agent_name": row["agent_name"],
                    "workspace_id": row["workspace_id"],
                    "start_time_ns": row["start_time_ns"],
                    "end_time_ns": row["end_time_ns"],
                    "duration_ms": row["duration_ms"],
                    "is_error": row["is_error"],
                    "total_tokens": row["total_tokens"],
                    "input_value": row["claim_input_value"],
                    "output_value": row["claim_output_value"],
                    "updated_at": updated_at,
                },
            )
            return

        replace_root = _should_replace_root(
            row["name"], row["parent_span_id"], existing["root_name"]
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
                    row["claim_root_span_id"] if replace_root else existing["root_span_id"]
                ),
                "root_name": (row["claim_root_name"] if replace_root else existing["root_name"]),
                "input_value": (
                    row["claim_input_value"] if replace_root else existing["input_value"]
                ),
                "output_value": (
                    row["claim_output_value"] if replace_root else existing["output_value"]
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
                conn.execute(_UPSERT_SPAN_SQL, row)
                self._upsert_trace(conn, row, is_new_span=existing is None)

    def _maybe_run_retention(self, *, force: bool = False) -> None:
        if self._config.retention_days <= 0 and self._config.max_spans <= 0:
            return
        now = time.monotonic()
        if not force and (now - self._last_retention_at) < _RETENTION_INTERVAL_SEC:
            return
        self._last_retention_at = now
        try:
            with self._lock:
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

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        try:
            for attempt, delay_ms in enumerate(_LOCK_RETRY_DELAYS_MS):
                try:
                    with self._lock:
                        self._export_batch(spans)
                    self._maybe_run_retention()
                    return SpanExportResult.SUCCESS
                except sqlite3.OperationalError as exc:
                    if not _is_lock_error(exc) or attempt == len(_LOCK_RETRY_DELAYS_MS) - 1:
                        raise
                    time.sleep(delay_ms / 1000.0)
            return SpanExportResult.FAILURE
        except Exception as exc:
            logger.warning("sqlite span export failed: %s", exc)
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        try:
            with self._lock:
                if self._conn is None:
                    return True
                self._conn.execute("pragma wal_checkpoint(PASSIVE)")
                self._last_retention_at = 0.0
                self._run_retention_locked(self._conn)
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
