"""Realtime session guardrails: max duration, idle timeout, max response turn.

Guardrails are enforced by a background task that runs alongside the main session loop.
They are non-blocking and only raise a :class:`GuardrailError` when a threshold is
crossed. The session loop then closes the session gracefully. The WebSocket ping/pong
keepalive is handled by the underlying Starlette/FastAPI implementation; this module only
encloses the application-level idle timer.
"""

from __future__ import annotations

import asyncio
import logging
import time

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.core.logging_utils import kv

from .errors import GuardrailError
from .session import RealtimeConnectionState

logger = logging.getLogger("monkeybot.gateway.realtime.guardrails")


async def run_guardrails(
    state: RealtimeConnectionState,
    config: RealtimeConfig,
    *,
    poll_interval_sec: float = 5.0,
) -> None:
    """Background task enforcing session duration, idle timeout, and response-turn limits.

    Raises :class:`GuardrailError` with the guardrail reason when a limit is hit. The
    caller is responsible for canceling this task after the error is raised.
    """
    max_duration = config.session.max_duration_sec
    idle_timeout = config.session.idle_timeout_sec
    max_response_turn = config.session.max_response_turn_sec

    while True:
        await asyncio.sleep(poll_interval_sec)
        if state._closed or state.state == "closing":
            return

        now = time.monotonic()

        # Max duration hard cap
        if now - state.opened_at >= max_duration:
            logger.info(
                "realtime guardrail triggered: max_duration %s",
                kv(session_id=state.session_id, max_duration_sec=max_duration),
            )
            raise GuardrailError(
                f"Session exceeded max_duration_sec={max_duration}",
                details="max_duration_exceeded",
            )

        # Idle timeout: no client or provider activity.
        if now - state.last_activity_at >= idle_timeout:
            logger.info(
                "realtime guardrail triggered: idle_timeout %s",
                kv(
                    session_id=state.session_id,
                    idle_timeout_sec=idle_timeout,
                    idle_sec=round(now - state.last_activity_at, 1),
                ),
            )
            raise GuardrailError(
                f"No activity for idle_timeout_sec={idle_timeout}",
                details="idle_timeout",
            )

        # Max response turn: one model turn cannot exceed the limit.
        if state.state in ("thinking", "speaking", "tool_running"):
            turn_start = state.metrics.current_turn_started_at if state.metrics else None
            if turn_start is not None and now - turn_start >= max_response_turn:
                logger.info(
                    "realtime guardrail triggered: max_response_turn %s",
                    kv(
                        session_id=state.session_id,
                        max_response_turn_sec=max_response_turn,
                        turn_sec=round(now - turn_start, 1),
                    ),
                )
                raise GuardrailError(
                    f"Model turn exceeded max_response_turn_sec={max_response_turn}",
                    details="max_response_turn_exceeded",
                )
