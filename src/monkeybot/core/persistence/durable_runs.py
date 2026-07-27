"""Durable subprocess run tracking in SQLite."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

import aiosqlite

_SUBAGENT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "parent_run_id",
    "script",
    "envelope_json",
    "status",
    "result_json",
    "error_json",
    "started_at",
    "finished_at",
    "scratch_dir",
    "worker_id",
    "claimed_at",
)


@dataclass(frozen=True)
class SubagentRunRow:
    """One row from the ``subagent_runs`` table."""

    run_id: str
    parent_run_id: str | None
    script: str
    envelope_json: str
    status: str
    result_json: str | None
    error_json: str | None
    started_at: int
    finished_at: int | None
    scratch_dir: str
    worker_id: str | None = None
    claimed_at: int | None = None


@dataclass
class SubagentEnvelope:
    """Minimal envelope persisted with each subagent run."""

    task: str
    context: str
    memory_storage_uri: str
    parent_run_id: str
    model: str = "gemini-2.5-flash"
    traceparent: str | None = None
    agent_md: str | None = None
    subagent_type: str | None = None
    parent_session_id: str | None = None
    child_thread_id: str | None = None

    def to_json(self) -> str:
        """Serialize envelope to JSON text; omit optional fields that are None."""
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> SubagentEnvelope:
        """Deserialize JSON text; raises ``ValueError`` on invalid input."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid envelope JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("Envelope JSON must be an object")
        base_required = {"task", "context", "parent_run_id"}
        missing = base_required - data.keys()
        if missing:
            raise ValueError(f"Envelope missing fields: {sorted(missing)}")
        from monkeybot.core.subagents.subagent_proto import _memory_storage_uri_from_dict

        uri = _memory_storage_uri_from_dict(data)
        model = data.get("model", "gemini-2.5-flash")
        if not isinstance(model, str):
            raise ValueError("model must be a string when present")
        for key in ("task", "context", "parent_run_id"):
            if not isinstance(data[key], str):
                raise ValueError(f"{key} must be a string")
        return cls(
            task=data["task"],
            context=data["context"],
            memory_storage_uri=uri.strip(),
            parent_run_id=data["parent_run_id"],
            model=model,
            traceparent=_opt_envelope_str(data, "traceparent"),
            agent_md=_opt_envelope_str(data, "agent_md"),
            subagent_type=_opt_envelope_str(data, "subagent_type"),
            parent_session_id=_opt_envelope_str(data, "parent_session_id"),
            child_thread_id=_opt_envelope_str(data, "child_thread_id"),
        )


def _opt_envelope_str(data: dict[str, object], key: str) -> str | None:
    """Optional string field: missing/blank → None; wrong type → ValueError."""
    if key not in data:
        return None
    val = data[key]
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"{key} must be a string when present")
    stripped = val.strip()
    return stripped or None


def _tuple_to_run_row(row: tuple[object, ...]) -> SubagentRunRow:
    d = dict(zip(_SUBAGENT_COLUMNS, row, strict=True))
    return SubagentRunRow(
        run_id=str(d["run_id"]),
        parent_run_id=str(d["parent_run_id"]) if d["parent_run_id"] is not None else None,
        script=str(d["script"]),
        envelope_json=str(d["envelope_json"]),
        status=str(d["status"]),
        result_json=str(d["result_json"]) if d["result_json"] is not None else None,
        error_json=str(d["error_json"]) if d["error_json"] is not None else None,
        started_at=int(cast(int, d["started_at"])),
        finished_at=int(cast(int, d["finished_at"])) if d["finished_at"] is not None else None,
        scratch_dir=str(d["scratch_dir"]),
        worker_id=str(d["worker_id"]) if d["worker_id"] is not None else None,
        claimed_at=int(cast(int, d["claimed_at"])) if d["claimed_at"] is not None else None,
    )


class SQLiteRunStore:
    """Persist lifecycle rows for subprocess subagents."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record_pending(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None:
        """Insert a ``pending`` row for worker-pool consumption."""
        await self._record_run("pending", run_id, parent_run_id, script, envelope, scratch_dir)

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None:
        """Insert a ``running`` row with envelope metadata."""
        await self._record_run("running", run_id, parent_run_id, script, envelope, scratch_dir)

    async def _record_run(
        self,
        status: str,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None:
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            INSERT INTO subagent_runs(
                run_id, parent_run_id, script, envelope_json,
                status, result_json, error_json, started_at, finished_at, scratch_dir,
                worker_id, claimed_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, NULL, NULL)
            """,
            (
                run_id,
                parent_run_id,
                script,
                envelope.to_json(),
                status,
                now_ms,
                str(scratch_dir),
            ),
        )
        await self._conn.commit()

    async def claim(self, run_id: str, worker_id: str) -> bool:
        """Atomically transition ``pending`` -> ``running``; return True if this caller won."""
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE subagent_runs
            SET status = 'running',
                worker_id = ?,
                claimed_at = ?
            WHERE run_id = ? AND status = 'pending'
            """,
            (worker_id, now_ms, run_id),
        )
        await self._conn.commit()
        return int(cursor.rowcount) == 1

    async def reset_stale_claims(self, stale_after_ms: int) -> int:
        """Reset ``running`` rows with stale ``claimed_at`` back to ``pending``."""
        cutoff = int(time.time() * 1000) - stale_after_ms
        cursor = await self._conn.execute(
            """
            UPDATE subagent_runs
            SET status = 'pending',
                worker_id = NULL,
                claimed_at = NULL
            WHERE status = 'running'
              AND claimed_at IS NOT NULL
              AND claimed_at < ?
            """,
            (cutoff,),
        )
        await self._conn.commit()
        return int(cursor.rowcount)

    async def record_completed(self, run_id: str, result_json: str) -> None:
        """Mark run completed with payload."""
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            UPDATE subagent_runs
            SET status = 'completed',
                result_json = ?,
                finished_at = ?,
                error_json = NULL
            WHERE run_id = ?
            """,
            (result_json, now_ms, run_id),
        )
        await self._conn.commit()

    async def record_failed(self, run_id: str, error: str) -> None:
        """Mark run failed with JSON ``{\"message\": ...}`` error."""
        now_ms = int(time.time() * 1000)
        err_payload = json.dumps({"message": error})
        await self._conn.execute(
            """
            UPDATE subagent_runs
            SET status = 'failed',
                error_json = ?,
                finished_at = ?
            WHERE run_id = ?
            """,
            (err_payload, now_ms, run_id),
        )
        await self._conn.commit()

    async def pending_runs(self) -> list[SubagentRunRow]:
        """Return ``pending`` runs oldest-first (worker pool claim candidates)."""
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM subagent_runs
            WHERE status = 'pending'
            ORDER BY started_at ASC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_tuple_to_run_row(tuple(row)) for row in rows]

    async def get_run(self, run_id: str) -> SubagentRunRow | None:
        """Return full row or ``None``."""
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM subagent_runs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _tuple_to_run_row(tuple(row))
