"""Native /goal lifecycle over scheduled-loop persistence."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable
from dataclasses import replace

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.core.persistence.scheduled_loops import (
    GOAL_DEFAULT_INTERVAL_MS,
    KIND_GOAL,
    OPEN_GOAL_STATUSES,
    OpenGoalExistsError,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    normalize_objective,
)

logger = logging.getLogger(__name__)


class GoalConflictError(ValueError):
    """Session already has a different open goal."""


class GoalNotFoundError(ValueError):
    """No matching goal for this session or id."""


def goal_to_json(row: ScheduledLoopRow) -> dict[str, object]:
    objective = (row.objective or row.prompt).strip()
    return {
        "id": row.loop_id,
        "session_id": row.session_id,
        "objective": objective,
        "status": row.status,
        "tick_index": row.tick_index,
        "interval_ms": row.interval_ms,
        "next_tick_at_ms": row.next_tick_at_ms,
        "last_tick_at_ms": row.last_tick_at_ms,
        "started_at_ms": row.started_at_ms,
        "last_error": row.last_error,
        "stop_reason": row.stop_reason,
        "tick_in_flight": row.tick_in_flight,
        "consecutive_error_count": row.consecutive_error_count,
    }


class DurableGoalService:
    """One open goal per origin session. Scheduler ticks are an implementation detail."""

    def __init__(self, store: ScheduledLoopStore) -> None:
        self._store = store

    async def list_goals(self) -> list[ScheduledLoopRow]:
        return await self._store.list_kind(KIND_GOAL)

    async def get(self, goal_id: str) -> ScheduledLoopRow | None:
        row = await self._store.get(goal_id)
        if row is None or row.kind != KIND_GOAL:
            return None
        return row

    async def open_for_session(self, session_id: str) -> ScheduledLoopRow | None:
        return await self._store.find_open(
            session_id=session_id,
            kind=KIND_GOAL,
            statuses=OPEN_GOAL_STATUSES,
        )

    async def create(self, *, objective: str, session_id: str) -> tuple[ScheduledLoopRow, bool]:
        objective = objective.strip()
        if not objective:
            raise ValueError("create_goal requires a non-empty objective")
        existing = await self.open_for_session(session_id)
        if existing is not None:
            return self._resolve_existing(existing, objective=objective, session_id=session_id)
        spec = ScheduledLoopCreate(
            prompt=objective,
            interval_ms=GOAL_DEFAULT_INTERVAL_MS,
            session_id=session_id,
            loop_id=f"goal-{uuid.uuid4().hex[:12]}",
            kind=KIND_GOAL,
            objective=objective,
        )
        try:
            row = await self._store.create(spec)
        except OpenGoalExistsError:
            # SQL backends enforce the invariant with a partial unique index;
            # Firestore serializes creation through a per-session lock document.
            # Re-read after a losing race to preserve idempotent create semantics.
            raced = await self.open_for_session(session_id)
            if raced is None:
                raise
            return self._resolve_existing(raced, objective=objective, session_id=session_id)
        logger.info(
            "goal created %s",
            kv(goal_id=row.loop_id, session_id=session_id, status=row.status),
        )
        return row, True

    async def update(self, *, goal_id: str, session_id: str, status: str) -> ScheduledLoopRow:
        if status not in {"active", "complete"}:
            raise ValueError("update_goal status must be active or complete")
        existing = await self._require_for_session(goal_id, session_id=session_id)
        if existing.status not in OPEN_GOAL_STATUSES:
            raise GoalNotFoundError(f"no open goal {goal_id} in this session")
        if status == "complete":
            row = await self._stop(existing, stop_reason="complete")
            logger.info(
                "goal completed %s",
                kv(goal_id=row.loop_id, session_id=session_id, status=row.status),
            )
            return row
        if existing.status == "active":
            return existing
        if existing.status != "paused":
            raise ValueError("update_goal can set active only when the user paused the goal")
        row = await self._resume(existing)
        logger.info(
            "goal resumed %s",
            kv(goal_id=row.loop_id, session_id=session_id, status=row.status),
        )
        return row

    async def pause(self, goal_id: str) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.status not in OPEN_GOAL_STATUSES:
            raise GoalNotFoundError(goal_id)
        if row.status == "paused":
            return row
        updated = await self._mutate(
            row,
            self._store.pause(goal_id),
            status="paused",
            stop_reason=row.stop_reason,
        )
        logger.info("goal paused %s", kv(goal_id=goal_id, status=updated.status))
        return updated

    async def resume(self, goal_id: str) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.status != "paused":
            raise GoalNotFoundError(f"paused goal not found: {goal_id}")
        updated = await self._resume(row)
        logger.info("goal resumed %s", kv(goal_id=goal_id, status=updated.status))
        return updated

    async def stop(self, goal_id: str) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.status in {"completed", "failed"}:
            return row
        updated = await self._stop(row, stop_reason="manual")
        logger.info(
            "goal stopped %s",
            kv(goal_id=goal_id, status=updated.status, stop_reason=updated.stop_reason),
        )
        return updated

    async def _require(self, goal_id: str) -> ScheduledLoopRow:
        row = await self.get(goal_id)
        if row is None:
            raise GoalNotFoundError(f"unknown goal: {goal_id}")
        return row

    async def _require_for_session(
        self,
        goal_id: str,
        *,
        session_id: str,
    ) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.session_id != session_id:
            raise GoalNotFoundError(f"no goal {goal_id} in this session")
        return row

    async def _stop(self, row: ScheduledLoopRow, *, stop_reason: str) -> ScheduledLoopRow:
        return await self._mutate(
            row,
            self._store.stop(row.loop_id, stop_reason=stop_reason),
            status="completed",
            stop_reason=stop_reason,
        )

    async def _resume(self, row: ScheduledLoopRow) -> ScheduledLoopRow:
        return await self._mutate(
            row,
            self._store.resume(row.loop_id),
            status="active",
            stop_reason=None,
            next_tick_at_ms=int(time.time() * 1000),
        )

    async def _mutate(
        self,
        row: ScheduledLoopRow,
        mutation: Awaitable[bool],
        *,
        status: str,
        stop_reason: str | None,
        next_tick_at_ms: int | None = None,
    ) -> ScheduledLoopRow:
        """Await a store transition and mirror it onto ``row``.

        Mirroring avoids a second read; ``next_tick_at_ms=None`` keeps the
        stored schedule. Every transition releases any tick claim.
        """
        if not await mutation:
            raise GoalNotFoundError(row.loop_id)
        return replace(
            row,
            status=status,
            stop_reason=stop_reason,
            next_tick_at_ms=(
                next_tick_at_ms if next_tick_at_ms is not None else row.next_tick_at_ms
            ),
            tick_in_flight=False,
            worker_id=None,
            claimed_at_ms=None,
        )

    @staticmethod
    def _resolve_existing(
        existing: ScheduledLoopRow,
        *,
        objective: str,
        session_id: str,
    ) -> tuple[ScheduledLoopRow, bool]:
        current = normalize_objective(existing.objective or existing.prompt)
        if current == normalize_objective(objective):
            logger.info(
                "goal create idempotent %s",
                kv(goal_id=existing.loop_id, session_id=session_id, status=existing.status),
            )
            return existing, False
        raise GoalConflictError(
            f"a goal is already {existing.status} in this session: {existing.loop_id}"
        )
