"""EventBus — best-effort publish/subscribe with per-handler timeout + isolation.

A raising or slow handler MUST NEVER break the agent. This is the foundation that lets
consumers safely bolt on Phoenix / DeepEval / OpenTelemetry without endangering runtime.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .events import EventKind, HarnessEvent

log = logging.getLogger("emonk.harness.event_bus")


class EventHandler(Protocol):
    async def handle(self, event: HarnessEvent) -> None: ...


class LoggingEventHandler:
    """Default handler: writes a structured log line per event."""

    name = "LoggingEventHandler"

    async def handle(self, event: HarnessEvent) -> None:
        log.info(
            "harness.event",
            extra={
                "run_id": event.run_id,
                "session_id": event.session_id,
                "kind": event.kind.value,
                "principal": event.principal.id,
                "payload": event.payload,
                "redacted": event.redacted,
            },
        )


@dataclass
class _Sub:
    handler: EventHandler
    kinds: frozenset[EventKind] | None
    timeout_s: float


@dataclass
class EventBusStats:
    published: int = 0
    delivered: int = 0
    handler_errors: int = 0
    handler_timeouts: int = 0
    slow_handlers: int = 0
    per_handler_errors: dict[str, int] = field(default_factory=dict)


class EventBus:
    def __init__(
        self,
        *,
        default_handler_timeout_s: float = 0.5,
        slow_handler_threshold_s: float = 0.05,
        include_default_logger: bool = True,
    ) -> None:
        self._subs: list[_Sub] = []
        self._default_timeout = default_handler_timeout_s
        self._slow_threshold = slow_handler_threshold_s
        self.stats = EventBusStats()
        if include_default_logger:
            self.subscribe(LoggingEventHandler(), timeout_s=0.1)

    def subscribe(
        self,
        handler: EventHandler,
        *,
        kinds: Iterable[EventKind] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._subs.append(
            _Sub(
                handler=handler,
                kinds=frozenset(kinds) if kinds else None,
                timeout_s=timeout_s if timeout_s is not None else self._default_timeout,
            )
        )

    def subscribers(self) -> list[EventHandler]:
        return [s.handler for s in self._subs]

    async def publish(self, event: HarnessEvent) -> None:
        self.stats.published += 1
        for sub in self._subs:
            if sub.kinds is not None and event.kind not in sub.kinds:
                continue
            await self._deliver(sub, event)

    async def _deliver(self, sub: _Sub, event: HarnessEvent) -> None:
        handler_name = getattr(sub.handler, "name", type(sub.handler).__name__)
        start = time.monotonic()
        try:
            await asyncio.wait_for(sub.handler.handle(event), timeout=sub.timeout_s)
            self.stats.delivered += 1
        except asyncio.TimeoutError:
            self.stats.handler_timeouts += 1
            self.stats.per_handler_errors[handler_name] = (
                self.stats.per_handler_errors.get(handler_name, 0) + 1
            )
            log.warning(
                "event handler %s timed out after %.3fs on %s",
                handler_name,
                sub.timeout_s,
                event.kind.value,
            )
        except Exception as exc:  # noqa: BLE001
            self.stats.handler_errors += 1
            self.stats.per_handler_errors[handler_name] = (
                self.stats.per_handler_errors.get(handler_name, 0) + 1
            )
            log.warning(
                "event handler %s raised on %s: %s",
                handler_name,
                event.kind.value,
                exc,
            )
        finally:
            elapsed = time.monotonic() - start
            if elapsed > self._slow_threshold:
                self.stats.slow_handlers += 1
