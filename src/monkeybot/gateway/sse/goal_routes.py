"""REST control plane for native durable goals."""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from monkeybot.core.goals.service import (
    DurableGoalService,
    GoalClosedError,
    GoalNotFoundError,
    GoalPausedError,
    goal_to_json,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.gateway.sse.models import APIError
from monkeybot.gateway.sse.request_storage import require_storage_backend

logger = logging.getLogger(__name__)

_WAKE_TOKEN_ENV = "MONKEYBOT_GOAL_WAKE_TOKEN"


def _goal_service(request: Request) -> DurableGoalService:
    store: ScheduledLoopStore = require_storage_backend(request).scheduled_loops()
    return DurableGoalService(store)


class WakeGoalRequest(BaseModel):
    reason: str = ""
    source: str = ""


_WAKE_BODY = Body(default_factory=WakeGoalRequest)


def _require_wake_token(request: Request) -> None:
    """Optional bearer token. Unset means local callers are not challenged."""
    expected = os.environ.get(_WAKE_TOKEN_ENV, "").strip()
    if not expected:
        return
    auth = request.headers.get("authorization") or ""
    prefix = "Bearer "
    presented = auth[len(prefix) :] if auth.startswith(prefix) else ""
    got_b = presented.encode()
    exp_b = expected.encode()
    matches = len(got_b) == len(exp_b) and hmac.compare_digest(got_b, exp_b)
    if not matches:
        raise APIError(
            401,
            "UNAUTHORIZED",
            "Goal wake authentication required",
            uuid.uuid4().hex,
        )


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

    @router.post("/{goal_id}/wake")
    async def wake_goal(
        goal_id: str,
        request: Request,
        body: WakeGoalRequest = _WAKE_BODY,
    ) -> dict[str, Any]:
        _require_wake_token(request)
        payload = body
        try:
            row = await _goal_service(request).wake(
                goal_id,
                reason=payload.reason,
                source=payload.source,
            )
        except GoalPausedError as exc:
            logger.warning("goal wake paused %s", kv(goal_id=goal_id, error=str(exc)))
            raise APIError(409, "GOAL_PAUSED", str(exc), uuid.uuid4().hex) from exc
        except GoalClosedError as exc:
            logger.warning("goal wake closed %s", kv(goal_id=goal_id, error=str(exc)))
            raise APIError(409, "GOAL_CLOSED", str(exc), uuid.uuid4().hex) from exc
        except GoalNotFoundError as exc:
            logger.warning("goal wake missed %s", kv(goal_id=goal_id, error=str(exc)))
            raise APIError(404, "GOAL_NOT_FOUND", str(exc), uuid.uuid4().hex) from exc
        logger.info("goal wake %s", kv(goal_id=goal_id, status=row.status))
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
