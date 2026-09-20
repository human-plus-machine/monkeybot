"""Durable scheduled agent loop records (prompt-first /loop-style automation)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import cast

import aiosqlite

from monkeybot.core.persistence.sqlite import TaskReentrantLock, with_conn_lock

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
    "kind",
    "objective",
    "consecutive_error_count",
)

_LOOP_STATUSES = frozenset({"active", "paused", "completed", "failed"})
KIND_LOOP = "loop"
KIND_GOAL = "goal"
OPEN_GOAL_STATUSES = frozenset({"active", "paused"})
GOAL_DEFAULT_INTERVAL_MS = 5 * 60 * 1000
GOAL_MAX_CONSECUTIVE_ERRORS = 3
# Prefix of every goal tick prompt; turn plumbing matches on it to re-advertise
# ``update_goal``, so both producer and consumer must share this literal.
GOAL_CONTINUATION_PREFIX = "[GOAL CONTINUATION · goal_id="

logger = logging.getLogger(__name__)


class OpenGoalExistsError(ValueError):
    """A backend rejected creation because the session already has an open goal."""


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
    kind: str = KIND_LOOP
    objective: str | None = None
    consecutive_error_count: int = 0


@dataclass(frozen=True)
class PlannedScheduledLoop:
    """Validated values shared by scheduled-loop persistence backends."""

    loop_id: str
    session_id: str
    prompt: str
    interval_ms: int
    max_ticks: int | None
    max_runtime_ms: int | None
    skip_if_busy: bool
    next_tick_at_ms: int
    started_at_ms: int
    kind: str
    objective: str | None


@dataclass(frozen=True)
class TickResolution:
    """State transition produced when a claimed tick finishes."""

    tick_index: int
    status: str
    stop_reason: str | None
    next_tick_at_ms: int
    last_error: str | None
    consecutive_error_count: int


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


def _kind_field(raw: object) -> str:
    if raw is None or not str(raw).strip():
        return KIND_LOOP
    value = str(raw).strip().lower()
    if value not in {KIND_LOOP, KIND_GOAL}:
        raise ValueError(f"invalid scheduled loop kind: {raw!r}")
    return value


def _optional_str_field(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def normalize_objective(text: str) -> str:
    return " ".join(text.split())


def planned_create(spec: ScheduledLoopCreate, *, now_ms: int) -> PlannedScheduledLoop:
    """Validate a create spec and return insert values shared by every backend."""
    kind = _kind_field(spec.kind)
    interval_ms = _require_positive_interval_ms("new", spec.interval_ms)
    if kind == KIND_GOAL:
        objective = (spec.objective or spec.prompt).strip()
        if not objective:
            raise ValueError("goals require a non-empty objective")
        prompt = spec.prompt.strip() or objective
        next_tick_at_ms = now_ms + interval_ms
        max_ticks = None
        max_runtime_ms = None
    else:
        validate_loop_guards(
            max_ticks=spec.max_ticks,
            max_runtime_ms=spec.max_runtime_ms,
            unbounded=spec.unbounded,
        )
        objective = None
        prompt = spec.prompt.strip()
        next_tick_at_ms = now_ms
        max_ticks = spec.max_ticks
        max_runtime_ms = spec.max_runtime_ms
    return PlannedScheduledLoop(
        loop_id=_loop_id_from_create(spec),
        session_id=spec.session_id.strip() or "loop-main",
        prompt=prompt,
        interval_ms=interval_ms,
        max_ticks=max_ticks,
        max_runtime_ms=max_runtime_ms,
        skip_if_busy=spec.skip_if_busy,
        next_tick_at_ms=next_tick_at_ms,
        started_at_ms=now_ms,
        kind=kind,
        objective=objective,
    )


def resolve_complete_tick(
    row: ScheduledLoopRow,
    *,
    error: str | None,
    now_ms: int,
) -> TickResolution:
    """Resolve a tick result, bounding retries for goals with persistent failures."""
    tick_index = row.tick_index + 1
    if error:
        error_count = row.consecutive_error_count + 1
        is_goal = row.kind == KIND_GOAL
        if is_goal and error_count < GOAL_MAX_CONSECUTIVE_ERRORS:
            return TickResolution(
                tick_index=tick_index,
                status="active",
                stop_reason=None,
                next_tick_at_ms=now_ms + row.interval_ms,
                last_error=error,
                consecutive_error_count=error_count,
            )
        return TickResolution(
            tick_index=tick_index,
            status="failed",
            stop_reason="consecutive_tick_errors" if is_goal else "tick_error",
            next_tick_at_ms=row.next_tick_at_ms,
            last_error=error,
            consecutive_error_count=error_count,
        )
    status = row.status
    stop_reason: str | None = None
    if row.max_ticks is not None and tick_index >= row.max_ticks:
        status = "completed"
        stop_reason = "max_ticks"
    elif row.max_runtime_ms is not None and (now_ms - row.started_at_ms) >= row.max_runtime_ms:
        status = "completed"
        stop_reason = "max_runtime"
    next_tick = now_ms + row.interval_ms if status == "active" else row.next_tick_at_ms
    return TickResolution(
        tick_index=tick_index,
        status=status,
        stop_reason=stop_reason,
        next_tick_at_ms=next_tick,
        last_error=None,
        consecutive_error_count=0,
    )


def _require_positive_interval_ms(loop_id: str, interval_ms: int) -> int:
    if interval_ms <= 0:
        raise ValueError(f"scheduled loop {loop_id} has invalid interval_ms: {interval_ms}")
    return interval_ms


def doc_to_scheduled_loop_row(loop_id: str, data: dict[str, object]) -> ScheduledLoopRow:
    """Map a Firestore document (or JSON blob) to :class:`ScheduledLoopRow`."""
    interval_ms = _require_positive_interval_ms(loop_id, int(cast(int, data.get("interval_ms", 0))))
    return ScheduledLoopRow(
        loop_id=loop_id,
        session_id=str(data.get("session_id", "")),
        status=str(data.get("status", "")),
        prompt=str(data.get("prompt", "")),
        interval_ms=interval_ms,
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
        kind=_kind_field(data.get("kind")),
        objective=_optional_str_field(data.get("objective")),
        consecutive_error_count=int(cast(int, data.get("consecutive_error_count", 0))),
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
    kind: str = KIND_LOOP
    objective: str | None = None


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
        raise ValueError("scheduled loops require max_ticks, max_runtime, or unbounded=true")


def _row_from_tuple(row: tuple[object, ...]) -> ScheduledLoopRow:
    d = dict(zip(_SCHEDULED_LOOP_COLUMNS, row, strict=True))
    loop_id = str(d["loop_id"])
    return ScheduledLoopRow(
        loop_id=loop_id,
        session_id=str(d["session_id"]),
        status=str(d["status"]),
        prompt=str(d["prompt"]),
        interval_ms=_require_positive_interval_ms(loop_id, int(cast(int, d["interval_ms"]))),
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
        kind=_kind_field(d.get("kind")),
        objective=_optional_str_field(d.get("objective")),
        consecutive_error_count=int(cast(int, d.get("consecutive_error_count", 0))),
    )


def _try_row_from_tuple(row: tuple[object, ...]) -> ScheduledLoopRow | None:
    """Map a SQL row, skipping (and logging) malformed records."""
    try:
        return _row_from_tuple(row)
    except ValueError as exc:
        logger.error("skipping malformed scheduled loop: %s", exc)
        return None


def _map_loop_tuples(rows: Iterable[Sequence[object]]) -> list[ScheduledLoopRow]:
    mapped_rows: list[ScheduledLoopRow] = []
    for raw in rows:
        mapped = _try_row_from_tuple(tuple(raw))
        if mapped is not None:
            mapped_rows.append(mapped)
    return mapped_rows


def _loop_id_from_create(spec: ScheduledLoopCreate) -> str:
    if spec.loop_id and spec.loop_id.strip():
        return spec.loop_id.strip()
    return f"loop-{uuid.uuid4().hex[:12]}"


def format_tick_prompt(row: ScheduledLoopRow) -> str:
    """Wrap the stored user prompt with tick metadata for each scheduled invocation."""
    if row.kind == KIND_GOAL:
        objective = (row.objective or row.prompt).strip()
        return (
            f"{GOAL_CONTINUATION_PREFIX}{row.loop_id} · session={row.session_id}]\n\n"
            f"Objective (keep this intact; do not shrink scope):\n{objective}\n\n"
            "This goal already exists. Do not call create_goal again.\n"
            "Do the next concrete increment of work now.\n"
            "Call update_goal with status complete only after evidence proves every "
            "requirement. If work remains, leave the goal active.\n"
        )
    max_label = str(row.max_ticks) if row.max_ticks is not None else "∞"
    tick_num = row.tick_index + 1
    header = (
        f"[SCHEDULED TICK {tick_num}/{max_label} · loop_id={row.loop_id} · "
        f"session={row.session_id}]"
    )
    return f"{header}\n\n{row.prompt.strip()}"


class SQLiteScheduledLoopStore:
    """SQLite persistence for scheduled agent loops."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        lock: asyncio.Lock | TaskReentrantLock | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or TaskReentrantLock()

    @with_conn_lock
    async def create(self, spec: ScheduledLoopCreate) -> ScheduledLoopRow:
        now_ms = int(time.time() * 1000)
        values = planned_create(spec, now_ms=now_ms)
        loop_id = values.loop_id
        try:
            await self._conn.execute(
                """
                INSERT INTO scheduled_loops(
                    loop_id, session_id, status, prompt, interval_ms,
                    max_ticks, max_runtime_ms, skip_if_busy, tick_index,
                    next_tick_at_ms, started_at_ms, last_tick_at_ms,
                    last_error, stop_reason, tick_in_flight, worker_id, claimed_at_ms,
                    kind, objective, consecutive_error_count
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, 0,
                          NULL, NULL, ?, ?, 0)
                """,
                (
                    loop_id,
                    values.session_id,
                    values.prompt,
                    values.interval_ms,
                    values.max_ticks,
                    values.max_runtime_ms,
                    1 if values.skip_if_busy else 0,
                    values.next_tick_at_ms,
                    values.started_at_ms,
                    values.kind,
                    values.objective,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            if await self.get(loop_id) is not None:
                raise ValueError(f"scheduled loop already exists: {loop_id}") from exc
            if values.kind == KIND_GOAL:
                raise OpenGoalExistsError(
                    f"an open goal already exists for session: {values.session_id}"
                ) from exc
            raise
        await self._conn.commit()
        row = await self.get(loop_id)
        if row is None:
            raise RuntimeError("failed to read scheduled loop after insert")
        return row

    @with_conn_lock
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
        return _try_row_from_tuple(tuple(row))

    @with_conn_lock
    async def list_all(self) -> list[ScheduledLoopRow]:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM scheduled_loops ORDER BY started_at_ms DESC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return _map_loop_tuples(rows)

    @with_conn_lock
    async def list_kind(self, kind: str) -> list[ScheduledLoopRow]:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM scheduled_loops
            WHERE kind = ?
            ORDER BY started_at_ms DESC
            """,
            (kind,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return _map_loop_tuples(rows)

    @with_conn_lock
    async def find_open(
        self,
        *,
        session_id: str,
        kind: str,
        statuses: Collection[str],
    ) -> ScheduledLoopRow | None:
        wanted = tuple(statuses)
        if not wanted:
            return None
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        placeholders = ", ".join("?" for _ in wanted)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM scheduled_loops
            WHERE kind = ? AND session_id = ? AND status IN ({placeholders})
            ORDER BY started_at_ms DESC
            LIMIT 1
            """,
            (kind, session_id, *wanted),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _try_row_from_tuple(tuple(row))

    @with_conn_lock
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
        return _map_loop_tuples(rows)

    @with_conn_lock
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

    @with_conn_lock
    async def release_stale_claims(self, stale_after_ms: int) -> int:
        """Reset in-flight ticks with no heartbeat after ``stale_after_ms``."""
        cutoff = int(time.time() * 1000) - stale_after_ms
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                status = CASE
                    WHEN kind = 'goal' AND consecutive_error_count + 1 >= ? THEN 'failed'
                    ELSE status
                END,
                stop_reason = CASE
                    WHEN kind = 'goal' AND consecutive_error_count + 1 >= ?
                        THEN 'consecutive_tick_errors'
                    ELSE stop_reason
                END,
                last_error = CASE
                    WHEN kind = 'goal' THEN 'stale tick claim released'
                    ELSE COALESCE(last_error, 'stale tick claim released')
                END,
                consecutive_error_count = CASE
                    WHEN kind = 'goal' THEN consecutive_error_count + 1
                    ELSE consecutive_error_count
                END
            WHERE tick_in_flight = 1
              AND claimed_at_ms IS NOT NULL
              AND claimed_at_ms < ?
            """,
            (GOAL_MAX_CONSECUTIVE_ERRORS, GOAL_MAX_CONSECUTIVE_ERRORS, cutoff),
        )
        await self._conn.commit()
        return int(cursor.rowcount)

    @with_conn_lock
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

    @with_conn_lock
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
        result = resolve_complete_tick(row, error=error, now_ms=now_ms)
        await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_index = ?, last_tick_at_ms = ?, last_error = ?,
                status = ?, stop_reason = ?, next_tick_at_ms = ?,
                tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                consecutive_error_count = ?
            WHERE loop_id = ? AND worker_id = ? AND tick_in_flight = 1
            """,
            (
                result.tick_index,
                now_ms,
                result.last_error,
                result.status,
                result.stop_reason,
                result.next_tick_at_ms,
                result.consecutive_error_count,
                loop_id,
                worker_id,
            ),
        )
        await self._conn.commit()
        return await self.get(loop_id)

    @with_conn_lock
    async def defer_tick(self, loop_id: str, *, worker_id: str, reason: str) -> bool:
        """Release claim and push next tick forward (e.g. session busy).

        ``WHERE worker_id`` + ``tick_in_flight = 1`` prevents clearing a claim that
        was stale-released and reclaimed by another worker between our read and write.
        """
        row = await self.get(loop_id)
        if row is None or row.worker_id != worker_id or not row.tick_in_flight:
            return False
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE scheduled_loops
            SET tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                next_tick_at_ms = ?,
                last_error = CASE WHEN kind = 'goal' THEN last_error ELSE ? END
            WHERE loop_id = ? AND worker_id = ? AND tick_in_flight = 1
            """,
            (now_ms + row.interval_ms, reason, loop_id, worker_id),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    @with_conn_lock
    async def set_status(
        self, loop_id: str, status: str, *, stop_reason: str | None = None
    ) -> bool:
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

    @with_conn_lock
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
            "kind": row.kind,
            "objective": row.objective,
            "consecutive_error_count": row.consecutive_error_count,
        }
