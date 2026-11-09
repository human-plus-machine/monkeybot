"""Native /goal lifecycle over scheduled-loop persistence."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import replace

from monkeybot.core.config.settings import GoalsConfig
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.core.persistence.scheduled_loops import (
    KIND_GOAL,
    OPEN_GOAL_STATUSES,
    OpenGoalExistsError,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    goal_ticks_remaining,
    normalize_objective,
)

_WAKE_TEXT_MAX = 2000

logger = logging.getLogger(__name__)


class GoalConflictError(ValueError):
    """Session already has a different open goal."""


class GoalNotFoundError(ValueError):
    """No matching goal for this session or id."""


class GoalPausedError(ValueError):
    """A wake arrived for a goal the user or the budget paused."""


class GoalClosedError(ValueError):
    """A wake arrived for a goal that is already completed or failed."""


def _completion_evidence(raw: str | None) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def goal_to_json(row: ScheduledLoopRow) -> dict[str, object]:
    objective = (row.objective or row.prompt).strip()
    return {
        "id": row.loop_id,
        "session_id": row.session_id,
        "objective": objective,
        "status": row.status,
        "tick_index": row.tick_index,
        "interval_ms": row.interval_ms,
        "max_ticks": row.max_ticks,
        "ticks_remaining": goal_ticks_remaining(row),
        "next_tick_at_ms": row.next_tick_at_ms,
        "last_tick_at_ms": row.last_tick_at_ms,
        "started_at_ms": row.started_at_ms,
        "last_error": row.last_error,
        "stop_reason": row.stop_reason,
        "tick_in_flight": row.tick_in_flight,
        "consecutive_error_count": row.consecutive_error_count,
        "wait_reason": row.wait_reason,
        "wake_at_ms": row.wake_at_ms,
        "wake_reason": row.wake_reason,
        "wake_source": row.wake_source,
        "completion_evidence": _completion_evidence(row.completion_evidence),
    }


class DurableGoalService:
    """One open goal per origin session. Scheduler ticks are an implementation detail."""

    def __init__(self, store: ScheduledLoopStore, *, goals: GoalsConfig | None = None) -> None:
        self._store = store
        self._goals = goals or GoalsConfig()

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
            interval_ms=self._goals.interval_ms,
            session_id=session_id,
            loop_id=f"goal-{uuid.uuid4().hex[:12]}",
            max_ticks=self._goals.max_ticks,
            max_runtime_ms=self._goals.max_runtime_ms,
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

    async def update(
        self,
        *,
        goal_id: str,
        session_id: str,
        status: str,
        reason: str | None = None,
        wake_at_ms: int | None = None,
        completion_evidence: str | None = None,
    ) -> ScheduledLoopRow:
        if status not in {"active", "complete", "waiting"}:
            raise ValueError("update_goal status must be active, waiting, or complete")
        existing = await self._require_for_session(goal_id, session_id=session_id)
        if existing.status not in OPEN_GOAL_STATUSES:
            raise GoalNotFoundError(f"no open goal {goal_id} in this session")
        if status == "complete":
            row = await self._stop(
                existing,
                stop_reason="complete",
                completion_evidence=completion_evidence,
            )
            logger.info(
                "goal completed %s",
                kv(goal_id=row.loop_id, session_id=session_id, status=row.status),
            )
            return row
        if status == "waiting":
            return await self._mark_waiting(
                existing,
                session_id=session_id,
                reason=reason,
                wake_at_ms=wake_at_ms,
            )
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

    async def wake(
        self,
        goal_id: str,
        *,
        reason: str = "",
        source: str = "",
        now_ms: int | None = None,
    ) -> ScheduledLoopRow:
        row = await self._require(goal_id)
        if row.status == "paused":
            raise GoalPausedError(f"goal {goal_id} is paused")
        if row.status not in {"active", "waiting"}:
            raise GoalClosedError(f"goal {goal_id} is {row.status}")
        clipped_reason = reason.strip()[:_WAKE_TEXT_MAX]
        clipped_source = source.strip()[:200] or "webhook"
        if not await self._store.wake(
            goal_id,
            reason=clipped_reason,
            source=clipped_source,
            now_ms=now_ms,
        ):
            raise GoalNotFoundError(f"goal {goal_id} could not be woken")
        updated = await self.get(goal_id)
        if updated is None:
            raise GoalNotFoundError(goal_id)
        logger.info(
            "goal woken %s",
            kv(goal_id=goal_id, status=updated.status, source=clipped_source),
        )
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

    async def _mark_waiting(
        self,
        row: ScheduledLoopRow,
        *,
        session_id: str,
        reason: str | None,
        wake_at_ms: int | None,
    ) -> ScheduledLoopRow:
        if row.status not in {"active", "waiting"}:
            raise ValueError("update_goal can set waiting only while the goal is active")
        text = (reason or "").strip()
        if not text:
            raise ValueError("update_goal waiting requires a reason")
        if not await self._store.set_waiting(row.loop_id, reason=text, wake_at_ms=wake_at_ms):
            raise GoalNotFoundError(row.loop_id)
        updated = await self.get(row.loop_id)
        if updated is None:
            raise GoalNotFoundError(row.loop_id)
        logger.info(
            "goal waiting %s",
            kv(goal_id=row.loop_id, session_id=session_id, wake_at_ms=wake_at_ms),
        )
        return updated

    async def _stop(
        self,
        row: ScheduledLoopRow,
        *,
        stop_reason: str,
        completion_evidence: str | None = None,
    ) -> ScheduledLoopRow:
        if not await self._store.stop(
            row.loop_id,
            stop_reason=stop_reason,
            completion_evidence=completion_evidence,
        ):
            raise GoalNotFoundError(row.loop_id)
        updated = await self.get(row.loop_id)
        if updated is None:
            raise GoalNotFoundError(row.loop_id)
        return updated

    async def _resume(self, row: ScheduledLoopRow) -> ScheduledLoopRow:
        if not await self._store.resume(row.loop_id):
            raise GoalNotFoundError(row.loop_id)
        updated = await self.get(row.loop_id)
        if updated is None:
            raise GoalNotFoundError(row.loop_id)
        return updated

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
