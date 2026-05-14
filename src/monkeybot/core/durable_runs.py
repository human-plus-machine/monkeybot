"""Durable subprocess run tracking in SQLite."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

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


@dataclass
class SubagentEnvelope:
    """Minimal envelope persisted with each subagent run."""

    task: str
    context: str
    memory_path: str
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
        required = {"task", "context", "memory_path", "parent_run_id"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Envelope missing fields: {sorted(missing)}")
        model = data.get("model", "gemini-2.5-flash")
        if not isinstance(model, str):
            raise ValueError("model must be a string when present")
        for key in required:
            if not isinstance(data[key], str):
                raise ValueError(f"{key} must be a string")
        return cls(
            task=data["task"],
            context=data["context"],
            memory_path=data["memory_path"],
            parent_run_id=data["parent_run_id"],
            model=model,
        )


def _tuple_to_run_dict(row: tuple[object, ...]) -> dict[str, object]:
    return dict(zip(_SUBAGENT_COLUMNS, row, strict=True))


class DurableRunStore:
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

    async def pending_runs(self) -> list[dict[str, object]]:
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
        return [_tuple_to_run_dict(tuple(row)) for row in rows]

    async def get_run(self, run_id: str) -> dict[str, object] | None:
        """Return full row dict or ``None``."""
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM subagent_runs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _tuple_to_run_dict(tuple(row))
