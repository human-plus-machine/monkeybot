"""Durable scheduled agent loop records (prompt-first /loop-style automation)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import cast

import aiosqlite

_SCHEDULED_LOOP_COLUMNS: tuple[str, ...] = (
    "loop_id",
    "session_id",
    "status",
    "prompt",
    "interval_ms",
    "max_ticks",
    "max_runtime_ms",
    "skip_if_busy",
    "tick_index",
    "next_tick_at_ms",
    "started_at_ms",
    "last_tick_at_ms",
    "last_error",
    "stop_reason",
    "tick_in_flight",
    "worker_id",
    "claimed_at_ms",
)

_LOOP_STATUSES = frozenset({"active", "paused", "completed", "failed"})


@dataclass(frozen=True)
class ScheduledLoopRow:
    """One row from the ``scheduled_loops`` table."""

    loop_id: str
    session_id: str
    status: str
    prompt: str
    interval_ms: int
    max_ticks: int | None
    max_runtime_ms: int | None
    skip_if_busy: bool
    tick_index: int
    next_tick_at_ms: int
    started_at_ms: int
    last_tick_at_ms: int | None
    last_error: str | None
    stop_reason: str | None
    tick_in_flight: bool
    worker_id: str | None = None
    claimed_at_ms: int | None = None


def _optional_int_field(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _bool_field(raw: object, *, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return default


def doc_to_scheduled_loop_row(loop_id: str, data: dict[str, object]) -> ScheduledLoopRow:
    """Map a Firestore document (or JSON blob) to :class:`ScheduledLoopRow`."""
    return ScheduledLoopRow(
        loop_id=loop_id,
        session_id=str(data.get("session_id", "")),
        status=str(data.get("status", "")),
        prompt=str(data.get("prompt", "")),
        interval_ms=int(cast(int, data.get("interval_ms", 0))),
        max_ticks=_optional_int_field(data.get("max_ticks")),
        max_runtime_ms=_optional_int_field(data.get("max_runtime_ms")),
        skip_if_busy=_bool_field(data.get("skip_if_busy"), default=True),
        tick_index=int(cast(int, data.get("tick_index", 0))),
        next_tick_at_ms=int(cast(int, data.get("next_tick_at_ms", 0))),
        started_at_ms=int(cast(int, data.get("started_at_ms", 0))),
        last_tick_at_ms=_optional_int_field(data.get("last_tick_at_ms")),
        last_error=str(data["last_error"]) if data.get("last_error") is not None else None,
        stop_reason=str(data["stop_reason"]) if data.get("stop_reason") is not None else None,
        tick_in_flight=_bool_field(data.get("tick_in_flight")),
        worker_id=str(data["worker_id"]) if data.get("worker_id") is not None else None,
        claimed_at_ms=_optional_int_field(data.get("claimed_at_ms")),
    )


@dataclass(frozen=True)
class ScheduledLoopCreate:
    """Arguments for registering a new scheduled loop."""

    prompt: str
    interval_ms: int
    session_id: str = "loop-main"
    loop_id: str | None = None
    max_ticks: int | None = None
    max_runtime_ms: int | None = None
    skip_if_busy: bool = True
    unbounded: bool = False


def validate_loop_guards(
    *,
    max_ticks: int | None,
    max_runtime_ms: int | None,
    unbounded: bool,
) -> None:
    """Require an explicit stop guard unless the operator opts into unbounded loops."""
    if unbounded:
        return
    if max_ticks is None and max_runtime_ms is None:
        raise ValueError(
            "scheduled loops require max_ticks, max_runtime, or unbounded=true"
        )


def _row_from_tuple(row: tuple[object, ...]) -> ScheduledLoopRow:
    d = dict(zip(_SCHEDULED_LOOP_COLUMNS, row, strict=True))
    return ScheduledLoopRow(
        loop_id=str(d["loop_id"]),
        session_id=str(d["session_id"]),
        status=str(d["status"]),
        prompt=str(d["prompt"]),
        interval_ms=int(cast(int, d["interval_ms"])),
        max_ticks=int(cast(int, d["max_ticks"])) if d["max_ticks"] is not None else None,
        max_runtime_ms=(
            int(cast(int, d["max_runtime_ms"])) if d["max_runtime_ms"] is not None else None
        ),
        skip_if_busy=bool(int(cast(int, d["skip_if_busy"]))),
        tick_index=int(cast(int, d["tick_index"])),
        next_tick_at_ms=int(cast(int, d["next_tick_at_ms"])),
        started_at_ms=int(cast(int, d["started_at_ms"])),
        last_tick_at_ms=(
            int(cast(int, d["last_tick_at_ms"])) if d["last_tick_at_ms"] is not None else None
        ),
        last_error=str(d["last_error"]) if d["last_error"] is not None else None,
        stop_reason=str(d["stop_reason"]) if d["stop_reason"] is not None else None,
        tick_in_flight=bool(int(cast(int, d["tick_in_flight"]))),
        worker_id=str(d["worker_id"]) if d["worker_id"] is not None else None,
        claimed_at_ms=(
            int(cast(int, d["claimed_at_ms"])) if d["claimed_at_ms"] is not None else None
        ),
    )


def _loop_id_from_create(spec: ScheduledLoopCreate) -> str:
    if spec.loop_id and spec.loop_id.strip():
        return spec.loop_id.strip()
    return f"loop-{uuid.uuid4().hex[:12]}"


def format_tick_prompt(row: ScheduledLoopRow) -> str:
    """Wrap the stored user prompt with tick metadata for each scheduled invocation."""
    max_label = str(row.max_ticks) if row.max_ticks is not None else "∞"
    tick_num = row.tick_index + 1
    header = (
        f"[SCHEDULED TICK {tick_num}/{max_label} · loop_id={row.loop_id} · "
        f"session={row.session_id}]"
    )
    return f"{header}\n\n{row.prompt.strip()}"


class SQLiteScheduledLoopStore:
    """SQLite persistence for scheduled agent loops."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create(self, spec: ScheduledLoopCreate) -> ScheduledLoopRow:
        loop_id = _loop_id_from_create(spec)
        existing = await self.get(loop_id)
        if existing is not None:
            raise ValueError(f"scheduled loop already exists: {loop_id}")
        validate_loop_guards(
            max_ticks=spec.max_ticks,
            max_runtime_ms=spec.max_runtime_ms,
            unbounded=spec.unbounded,
        )
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            INSERT INTO scheduled_loops(
                loop_id, session_id, status, prompt, interval_ms,
                max_ticks, max_runtime_ms, skip_if_busy, tick_index,
                next_tick_at_ms, started_at_ms, last_tick_at_ms,
                last_error, stop_reason, tick_in_flight, worker_id, claimed_at_ms
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, 0, NULL, NULL)
            """,
            (
                loop_id,
                spec.session_id.strip() or "loop-main",
                spec.prompt.strip(),
                spec.interval_ms,
                spec.max_ticks,
                spec.max_runtime_ms,
                1 if spec.skip_if_busy else 0,
                now_ms,
                now_ms,
            ),
        )
        await self._conn.commit()
        row = await self.get(loop_id)
        if row is None:
            raise RuntimeError("failed to read scheduled loop after insert")
        return row

    async def get(self, loop_id: str) -> ScheduledLoopRow | None:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM scheduled_loops WHERE loop_id = ?",
            (loop_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _row_from_tuple(tuple(row))

    async def list_all(self) -> list[ScheduledLoopRow]:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM scheduled_loops ORDER BY started_at_ms DESC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_from_tuple(tuple(r)) for r in rows]

    async def list_due(self, now_ms: int) -> list[ScheduledLoopRow]:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM scheduled_loops
            WHERE status = 'active'
              AND tick_in_flight = 0
              AND next_tick_at_ms <= ?
            ORDER BY next_tick_at_ms ASC
            """,
            (now_ms,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_from_tuple(tuple(r)) for r in rows]

    async def claim_tick(self, loop_id: str, worker_id: str) -> ScheduledLoopRow | None:
        """Atomically mark a due loop as in-flight; returns None if not claimable."""
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_in_flight = 1, worker_id = ?, claimed_at_ms = ?
            WHERE loop_id = ?
              AND status = 'active'
              AND tick_in_flight = 0
              AND next_tick_at_ms <= ?
            """,
            (worker_id, now_ms, loop_id, now_ms),
        )
        await self._conn.commit()
        if cursor.rowcount != 1:
            return None
        return await self.get(loop_id)

    async def release_stale_claims(self, stale_after_ms: int) -> int:
        """Reset in-flight ticks with no heartbeat after ``stale_after_ms``."""
        cutoff = int(time.time() * 1000) - stale_after_ms
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                last_error = COALESCE(last_error, 'stale tick claim released')
            WHERE tick_in_flight = 1
              AND claimed_at_ms IS NOT NULL
              AND claimed_at_ms < ?
            """,
            (cutoff,),
        )
        await self._conn.commit()
        return int(cursor.rowcount)

    async def renew_tick_claim(self, loop_id: str, worker_id: str) -> bool:
        """Extend the in-flight claim lease for a worker that is still executing a tick."""
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET claimed_at_ms = ?
            WHERE loop_id = ?
              AND worker_id = ?
              AND tick_in_flight = 1
            """,
            (now_ms, loop_id, worker_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def complete_tick(
        self,
        loop_id: str,
        *,
        worker_id: str,
        error: str | None = None,
    ) -> ScheduledLoopRow | None:
        row = await self.get(loop_id)
        if row is None or not row.tick_in_flight or row.worker_id != worker_id:
            return None
        now_ms = int(time.time() * 1000)
        tick_index = row.tick_index + 1
        stop_reason: str | None = None
        status = row.status
        if error:
            status = "failed"
            stop_reason = "tick_error"
        elif row.max_ticks is not None and tick_index >= row.max_ticks:
            status = "completed"
            stop_reason = "max_ticks"
        elif row.max_runtime_ms is not None and (now_ms - row.started_at_ms) >= row.max_runtime_ms:
            status = "completed"
            stop_reason = "max_runtime"
        next_tick = now_ms + row.interval_ms if status == "active" else row.next_tick_at_ms
        await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_index = ?, last_tick_at_ms = ?, last_error = ?,
                status = ?, stop_reason = ?, next_tick_at_ms = ?,
                tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
            WHERE loop_id = ? AND worker_id = ? AND tick_in_flight = 1
            """,
            (
                tick_index,
                now_ms,
                error,
                status,
                stop_reason,
                next_tick,
                loop_id,
                worker_id,
            ),
        )
        await self._conn.commit()
        return await self.get(loop_id)

    async def defer_tick(self, loop_id: str, *, worker_id: str, reason: str) -> None:
        """Release claim and push next tick forward (e.g. session busy).

        ``WHERE worker_id`` + ``tick_in_flight = 1`` prevents clearing a claim that
        was stale-released and reclaimed by another worker between our read and write.
        """
        row = await self.get(loop_id)
        if row is None or row.worker_id != worker_id or not row.tick_in_flight:
            return
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                next_tick_at_ms = ?, last_error = ?
            WHERE loop_id = ? AND worker_id = ? AND tick_in_flight = 1
            """,
            (now_ms + row.interval_ms, reason, loop_id, worker_id),
        )
        await self._conn.commit()

    async def set_status(self, loop_id: str, status: str, *, stop_reason: str | None = None) -> bool:
        if status not in _LOOP_STATUSES:
            raise ValueError(f"invalid loop status: {status}")
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET status = ?, stop_reason = COALESCE(?, stop_reason),
                tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
            WHERE loop_id = ?
            """,
            (status, stop_reason, loop_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def pause(self, loop_id: str) -> bool:
        return await self.set_status(loop_id, "paused")

    async def resume(self, loop_id: str) -> bool:
        row = await self.get(loop_id)
        if row is None:
            return False
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET status = 'active', stop_reason = NULL, next_tick_at_ms = ?,
                tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
            WHERE loop_id = ? AND status = 'paused'
            """,
            (now_ms, loop_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def stop(self, loop_id: str, *, stop_reason: str = "manual") -> bool:
        return await self.set_status(loop_id, "completed", stop_reason=stop_reason)

    def row_to_json(self, row: ScheduledLoopRow) -> dict[str, object]:
        return {
            "loop_id": row.loop_id,
            "session_id": row.session_id,
            "status": row.status,
            "prompt": row.prompt,
            "interval_ms": row.interval_ms,
            "max_ticks": row.max_ticks,
            "max_runtime_ms": row.max_runtime_ms,
            "skip_if_busy": row.skip_if_busy,
            "tick_index": row.tick_index,
            "next_tick_at_ms": row.next_tick_at_ms,
            "started_at_ms": row.started_at_ms,
            "last_tick_at_ms": row.last_tick_at_ms,
            "last_error": row.last_error,
            "stop_reason": row.stop_reason,
            "tick_in_flight": row.tick_in_flight,
        }
