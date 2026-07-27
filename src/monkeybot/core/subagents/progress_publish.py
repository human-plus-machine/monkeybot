"""Nested subagent progress publishing helpers (Story 2).

Safe publish isolates parent SSE failures from the ``task`` tool. AssistantDelta
coalescing bounds parent-bus traffic (≤66ms or ≤512 chars, flush on boundaries
and on child ``request_id`` changes).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
from typing import TYPE_CHECKING

from monkeybot.core.runtime.events import (
    AgentEvent,
    AssistantDelta,
    ToolCallResult,
    is_subagent_forwardable,
    wrap_subagent_event,
)

if TYPE_CHECKING:
    from monkeybot.core.context import EventPublisherPort

logger = logging.getLogger(__name__)

ASSISTANT_DELTA_COALESCE_MS = 66
ASSISTANT_DELTA_COALESCE_CHARS = 512
NESTED_TOOL_RESULT_SNIPPET = 600
# First failure always logs; then every Nth thereafter (avoids silent blackout).
PUBLISH_WARN_EVERY = 25


def _sse_enabled() -> bool:
    """Return False when nested SSE progress is explicitly disabled."""
    raw = os.environ.get("MONKEYBOT_SUBAGENT_SSE", "1").strip().lower()
    return raw not in {"0", "false", "no"}


async def safe_publish(
    publisher: EventPublisherPort | None,
    event: AgentEvent,
    *,
    fail_count: list[int],
    run_id: str,
) -> None:
    """No-op if publisher is None. Catch all exceptions; warn on 1st and every Nth."""
    if publisher is None or not _sse_enabled():
        return
    try:
        await publisher.publish_event(event)
    except Exception:
        fail_count[0] += 1
        n = fail_count[0]
        if n == 1 or n % PUBLISH_WARN_EVERY == 0:
            logger.warning(
                "subagent progress publish failed run_id=%s fail_count=%s",
                run_id,
                n,
                exc_info=True,
            )


class AssistantDeltaCoalescer:
    """Buffer AssistantDelta for ≤66ms or ≤512 chars; flush on boundaries / close."""

    def __init__(
        self,
        *,
        publisher: EventPublisherPort | None,
        correlation: dict[str, str | None],
        fail_count: list[int],
        loop: asyncio.AbstractEventLoop | None = None,
        coalesce_ms: int = ASSISTANT_DELTA_COALESCE_MS,
        coalesce_chars: int = ASSISTANT_DELTA_COALESCE_CHARS,
    ) -> None:
        self._publisher = publisher if _sse_enabled() else None
        self._correlation = correlation
        self._fail_count = fail_count
        self._loop = loop
        self._coalesce_ms = coalesce_ms
        self._coalesce_chars = coalesce_chars
        self._buffer: list[str] = []
        self._delta_request_id = ""
        self._timer: asyncio.Task[None] | None = None

    async def handle(self, inner: AgentEvent) -> None:
        """Forward allowlisted events; coalesce AssistantDelta; truncate ToolCallResult.result."""
        if not is_subagent_forwardable(inner):
            return
        if isinstance(inner, AssistantDelta):
            # Flush before mixing deltas from a new child request/turn into the buffer.
            if self._buffer and inner.request_id != self._delta_request_id:
                await self.flush()
            if not self._buffer:
                self._delta_request_id = inner.request_id
            self._buffer.append(inner.delta)
            if sum(len(part) for part in self._buffer) >= self._coalesce_chars:
                await self.flush()
            else:
                self._schedule_flush()
            return
        await self.flush()
        to_wrap: AgentEvent = inner
        if isinstance(inner, ToolCallResult):
            result = inner.result or ""
            if len(result) > NESTED_TOOL_RESULT_SNIPPET:
                to_wrap = dataclasses.replace(
                    inner,
                    result=result[:NESTED_TOOL_RESULT_SNIPPET] + "…",
                )
        wrapped = wrap_subagent_event(
            request_id=self._correlation.get("request_id") or "",
            parent_call_id=self._correlation.get("parent_call_id") or "",
            run_id=self._correlation.get("run_id") or "",
            child_thread_id=self._correlation.get("child_thread_id") or "",
            subagent_type=self._correlation.get("subagent_type"),
            inner=to_wrap,
        )
        if wrapped is not None:
            await safe_publish(
                self._publisher,
                wrapped,
                fail_count=self._fail_count,
                run_id=self._correlation.get("run_id") or "",
            )

    def _schedule_flush(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        delay = self._coalesce_ms / 1000.0

        async def _timed() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            self._timer = None
            await self._flush_buffer()

        if self._loop is not None:
            self._timer = self._loop.create_task(_timed())
        else:
            self._timer = asyncio.create_task(_timed())

    async def flush(self) -> None:
        """Publish buffered delta if any; cancel pending timer."""
        timer = self._timer
        self._timer = None
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
        await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        delta = AssistantDelta(request_id=self._delta_request_id, delta=text)
        wrapped = wrap_subagent_event(
            request_id=self._correlation.get("request_id") or "",
            parent_call_id=self._correlation.get("parent_call_id") or "",
            run_id=self._correlation.get("run_id") or "",
            child_thread_id=self._correlation.get("child_thread_id") or "",
            subagent_type=self._correlation.get("subagent_type"),
            inner=delta,
        )
        if wrapped is not None:
            await safe_publish(
                self._publisher,
                wrapped,
                fail_count=self._fail_count,
                run_id=self._correlation.get("run_id") or "",
            )

    async def aclose(self) -> None:
        """Flush remaining buffer (call at drain end)."""
        await self.flush()
