"""ObservabilityMW — emits HarnessEvents at LLM and tool call boundaries.

This is a "bookend" middleware: the assembler calls ``on_llm_call / on_llm_result``
and ``on_tool_call / on_tool_result`` directly, rather than wrapping the deep-agent
graph. This keeps us robust across deep_agents versions.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from ..event_bus import EventBus
from ..events import EventKind, HarnessEvent, Principal, VersionTriple


class ObservabilityMW:
    name = "ObservabilityMW"

    def __init__(self, event_bus: EventBus, versions: VersionTriple) -> None:
        self.event_bus = event_bus
        self.versions = versions

    async def emit(
        self,
        kind: EventKind,
        *,
        run_id: str,
        session_id: str,
        principal: Principal,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.event_bus.publish(
            HarnessEvent(
                run_id=run_id,
                session_id=session_id,
                principal=principal,
                versions=self.versions,
                ts=datetime.now(UTC),
                kind=kind,
                payload=payload or {},
            )
        )

    async def on_llm_call(self, **kwargs: Any) -> float:
        await self.emit(EventKind.LLM_CALL, **kwargs)
        return time.monotonic()

    async def on_llm_result(self, start: float, **kwargs: Any) -> None:
        payload = dict(kwargs.pop("payload", {}))
        payload["latency_ms"] = int((time.monotonic() - start) * 1000)
        await self.emit(EventKind.LLM_RESULT, payload=payload, **kwargs)

    async def on_tool_call(self, **kwargs: Any) -> float:
        await self.emit(EventKind.TOOL_CALL, **kwargs)
        return time.monotonic()

    async def on_tool_result(self, start: float, **kwargs: Any) -> None:
        payload = dict(kwargs.pop("payload", {}))
        payload["latency_ms"] = int((time.monotonic() - start) * 1000)
        await self.emit(EventKind.TOOL_RESULT, payload=payload, **kwargs)
