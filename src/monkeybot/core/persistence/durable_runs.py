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


@dataclass
class SubagentEnvelope:
    """Minimal envelope persisted with each subagent run."""

    task: str
    context: str
    memory_storage_uri: str
    parent_run_id: str
    model: str = "gemini-2.5-flash"

    def to_json(self) -> str:
        """Serialize envelope to JSON text."""
        return json.dumps(asdict(self), sort_keys=True)

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
        uri = data.get("memory_storage_uri")
        if not isinstance(uri, str) or not uri.strip():
            legacy = data.get("memory_path")
            if isinstance(legacy, str) and legacy.strip():
                lp = legacy.strip()
                if lp.startswith("gcs://") or lp.startswith("s3://") or lp.startswith("local://"):
                    uri = lp
                else:
                    uri = "local://" + lp
            else:
                raise ValueError("Envelope missing memory_storage_uri or legacy memory_path")
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
        )


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
    )


class SQLiteRunStore:
    """Persist lifecycle rows for subprocess subagents."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None:
        """Insert a ``running`` row with envelope metadata."""
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            INSERT INTO subagent_runs(
                run_id, parent_run_id, script, envelope_json,
                status, result_json, error_json, started_at, finished_at, scratch_dir
            )
            VALUES (?, ?, ?, ?, 'running', NULL, NULL, ?, NULL, ?)
            """,
            (
                run_id,
                parent_run_id,
                script,
                envelope.to_json(),
                now_ms,
                str(scratch_dir),
            ),
        )
        await self._conn.commit()

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
        """Runs stuck in ``pending`` or ``running``."""
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM subagent_runs
            WHERE status IN ('pending','running')
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


# Backwards-compat alias — DurableRunStore was renamed to SQLiteRunStore in Step 1.5.
DurableRunStore = SQLiteRunStore
