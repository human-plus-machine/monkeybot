"""Gateway adapters for the scheduled-loop engine."""

from __future__ import annotations

import logging
import time

from monkeybot.core.config.snapshot import current_env_or_none
from monkeybot.core.persistence.backends import SessionTurnLockStore, StorageBackend
from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.gateway.sse.loop_port import LoopPort
from monkeybot.gateway.sse.session_bus import SessionAlreadyExistsError, SessionRegistry
from monkeybot.scheduler.tick_result import TickInvokeResult

logger = logging.getLogger(__name__)


class StorageSessionBusyChecker:
    """Check session busy state via durable storage (multi-replica safe)."""

    def __init__(self, turn_locks: SessionTurnLockStore) -> None:
        self._turn_locks = turn_locks

    def is_busy(self, session_id: str) -> bool:
        del session_id
        return False

    async def is_busy_async(self, session_id: str) -> bool:
        return await self._turn_locks.is_busy(session_id)


class GatewaySessionBusyChecker:
    """Legacy in-process checker; prefer :class:`StorageSessionBusyChecker`."""

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
        agent_md = current_env_or_none("AGENT_MD")
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
    """Invoke ``LoopPort.start_turn`` and surface turn-level outcomes."""

    def __init__(
        self,
        loop_port: LoopPort,
        registry: SessionRegistry,
        turn_locks: SessionTurnLockStore,
    ) -> None:
        self._loop_port = loop_port
        self._registry = registry
        self._turn_locks = turn_locks

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
        acquired = await self._turn_locks.try_acquire(session_id, request_id)
        if not acquired:
            return TickInvokeResult.session_busy()
        bus = self._registry.get(session_id)
        if bus is not None:
            bus.current_request_id = request_id
        try:
            await self._loop_port.start_turn(session_id, request_id, user_content)
        except Exception as exc:
            return TickInvokeResult.fail(str(exc))
        finally:
            await self._turn_locks.release(session_id, request_id)
            bus = self._registry.get(session_id)
            if bus is not None and bus.current_request_id == request_id:
                bus.current_request_id = None
        return TickInvokeResult.ok()
