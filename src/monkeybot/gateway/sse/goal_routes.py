"""REST control plane for native durable goals."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request

from monkeybot.core.goals.service import (
    DurableGoalService,
    GoalNotFoundError,
    goal_to_json,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import ScheduledLoopStore, StorageBackend
from monkeybot.gateway.sse.models import APIError

logger = logging.getLogger(__name__)


def _storage_backend(request: Request) -> StorageBackend:
    backend: StorageBackend | None = getattr(request.app.state, "storage", None)
    if backend is None:
        raise APIError(
            503, "STORAGE_NOT_READY", "Storage backend not initialized", uuid.uuid4().hex
        )
    return backend


def _goal_service(request: Request) -> DurableGoalService:
    store: ScheduledLoopStore = _storage_backend(request).scheduled_loops()
    return DurableGoalService(store)


def build_goals_router() -> APIRouter:
    router = APIRouter(prefix="/goals", tags=["goals"])

    @router.get("")
    async def list_goals(request: Request) -> dict[str, Any]:
        rows = await _goal_service(request).list_goals()
        return {"goals": [goal_to_json(row) for row in rows]}

    @router.get("/{goal_id}")
    async def get_goal(goal_id: str, request: Request) -> dict[str, Any]:
        row = await _goal_service(request).get(goal_id)
        if row is None:
            logger.warning("goal get missed %s", kv(goal_id=goal_id))
            raise APIError(404, "GOAL_NOT_FOUND", f"Unknown goal {goal_id}", uuid.uuid4().hex)
        return {"goal": goal_to_json(row)}

    @router.post("/{goal_id}/pause")
    async def pause_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            row = await _goal_service(request).pause(goal_id)
        except GoalNotFoundError as exc:
            logger.warning("goal pause missed %s", kv(goal_id=goal_id, error=str(exc)))
            raise APIError(404, "GOAL_NOT_FOUND", str(exc), uuid.uuid4().hex) from exc
        logger.info("goal pause %s", kv(goal_id=goal_id, status=row.status))
        return {"goal": goal_to_json(row)}

    @router.post("/{goal_id}/resume")
    async def resume_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            row = await _goal_service(request).resume(goal_id)
        except GoalNotFoundError as exc:
            logger.warning("goal resume missed %s", kv(goal_id=goal_id, error=str(exc)))
            raise APIError(404, "GOAL_NOT_FOUND", str(exc), uuid.uuid4().hex) from exc
        logger.info("goal resume %s", kv(goal_id=goal_id, status=row.status))
        return {"goal": goal_to_json(row)}

    @router.post("/{goal_id}/stop")
    async def stop_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            row = await _goal_service(request).stop(goal_id)
        except GoalNotFoundError as exc:
            logger.warning("goal stop missed %s", kv(goal_id=goal_id, error=str(exc)))
            raise APIError(404, "GOAL_NOT_FOUND", str(exc), uuid.uuid4().hex) from exc
        logger.info("goal stop %s", kv(goal_id=goal_id, status=row.status))
        return {"goal": goal_to_json(row)}

    return router
