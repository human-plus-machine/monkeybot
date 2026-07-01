"""Gateway adapters for the scheduled-loop engine."""

from __future__ import annotations

import logging
import os
import time

from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.gateway.sse.loop_port import LoopPort
from monkeybot.gateway.sse.session_bus import SessionAlreadyExistsError, SessionRegistry

logger = logging.getLogger(__name__)


class GatewaySessionBusyChecker:
    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    def is_busy(self, session_id: str) -> bool:
        bus = self._registry.get(session_id)
        return bus is not None and bus.current_request_id is not None


class GatewaySessionEnsurer:
    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    async def ensure_session(self, session_id: str) -> None:
        if self._registry.get(session_id) is not None:
            return
        agent_md = os.environ.get("AGENT_MD")
        try:
            self._registry.create(
                session_id,
                agent_md=agent_md,
                created_at_ms=int(time.time() * 1000),
            )
            logger.info("scheduler created session_id=%s", session_id)
        except SessionAlreadyExistsError:
            return


class GatewayTickInvoker:
    """Invoke ``LoopPort.start_turn`` and surface turn-level errors."""

    def __init__(self, loop_port: LoopPort, registry: SessionRegistry) -> None:
        self._loop_port = loop_port
        self._registry = registry

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> str | None:
        bus = self._registry.get(session_id)
        if bus is not None and bus.current_request_id is not None:
            return "session busy"
        if bus is not None:
            bus.current_request_id = request_id
        try:
            await self._loop_port.start_turn(session_id, request_id, user_content)
        except Exception as exc:
            return str(exc)
        finally:
            bus = self._registry.get(session_id)
            if bus is not None and bus.current_request_id == request_id:
                bus.current_request_id = None
        return None
