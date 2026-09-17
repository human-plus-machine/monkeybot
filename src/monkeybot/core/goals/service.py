"""Native /goal lifecycle over scheduled-loop persistence."""

from __future__ import annotations

import logging
import uuid

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.core.persistence.scheduled_loops import (
    GOAL_DEFAULT_INTERVAL_MS,
    KIND_GOAL,
    OPEN_GOAL_STATUSES,
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
        spec = ScheduledLoopCreate(
            prompt=objective,
            interval_ms=GOAL_DEFAULT_INTERVAL_MS,
            session_id=session_id,
            loop_id=f"goal-{uuid.uuid4().hex[:12]}",
            kind=KIND_GOAL,
            objective=objective,
        )
        row = await self._store.create(spec)
        logger.info(
            "goal created %s",
            kv(goal_id=row.loop_id, session_id=session_id, status=row.status),
        )
        return row, True

    async def update(self, *, session_id: str, status: str) -> ScheduledLoopRow:
        if status not in {"active", "complete"}:
            raise ValueError("update_goal status must be active or complete")
        existing = await self.open_for_session(session_id)
        if existing is None:
            raise GoalNotFoundError("no open goal in this session")
        if status == "complete":
            await self._store.stop(existing.loop_id, stop_reason="complete")
            row = await self.get(existing.loop_id)
            if row is None:
                raise GoalNotFoundError(existing.loop_id)
            logger.info(
                "goal completed %s",
                kv(goal_id=row.loop_id, session_id=session_id, status=row.status),
            )
            return row
        if existing.status == "active":
            return existing
        if existing.status != "paused":
            raise ValueError("update_goal can set active only when the user paused the goal")
        ok = await self._store.resume(existing.loop_id)
        if not ok:
            raise GoalNotFoundError(existing.loop_id)
        row = await self.get(existing.loop_id)
        if row is None:
            raise GoalNotFoundError(existing.loop_id)
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
        if not await self._store.pause(goal_id):
            raise GoalNotFoundError(goal_id)
        updated = await self.get(goal_id)
        if updated is None:
            raise GoalNotFoundError(goal_id)
        logger.info("goal paused %s", kv(goal_id=goal_id, status=updated.status))
        return updated

    async def resume(self, goal_id: str) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.status != "paused":
            raise GoalNotFoundError(f"paused goal not found: {goal_id}")
        if not await self._store.resume(goal_id):
            raise GoalNotFoundError(f"paused goal not found: {goal_id}")
        updated = await self.get(goal_id)
        if updated is None:
            raise GoalNotFoundError(goal_id)
        logger.info("goal resumed %s", kv(goal_id=goal_id, status=updated.status))
        return updated

    async def stop(self, goal_id: str) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.status in {"completed", "failed"}:
            return row
        if not await self._store.stop(goal_id, stop_reason="manual"):
            raise GoalNotFoundError(goal_id)
        updated = await self.get(goal_id)
        if updated is None:
            raise GoalNotFoundError(goal_id)
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
