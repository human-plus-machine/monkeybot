"""In-process lifecycle hooks for the agent loop.

Hooks are async callbacks registered against well-known points in the agent
loop (turn start/end, tool call before/after, provider request/response, etc.).
They are the extensibility point used by the memory subsystem, but the manager
itself is memory-agnostic.

Design rules (mirrored from rohitg00/agentmemory's hook contract):

* **Bounded.** Every hook call is wrapped in ``asyncio.wait_for`` (or scheduled
  as fire-and-forget when ``timeout_s == 0``). Hooks cannot stall the loop.
* **Silent.** Hook errors and timeouts are logged at WARNING and never
  propagate. A broken hook never breaks a turn.
* **Re-entrancy safe.** Hooks running inside ``fire()`` cannot recursively
  trigger hooks; the inner call returns the payload unchanged.
* **Mutable payload.** Hooks may set ``inject_text`` / ``inject_memory_lines``
  on the shared :class:`HookPayload`; later hooks for the same event observe
  and may extend those fields. Provider/tool hooks may also replace
  ``provider_messages`` / ``tools``.

This module has no dependency on :mod:`monkeybot.core.memory` or the agent
loop; both consume it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from monkeybot.core.context import TurnContext
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.types.types_tools import ToolDef

_log = logging.getLogger(__name__)


class HookEvent(StrEnum):
    """Lifecycle points at which hooks may fire.

    Read-side events (``USER_MESSAGE``, ``PRE_TURN``, ``PRE_TOOL``,
    ``TOOL_DEFINITION``, ``BEFORE_PROVIDER_REQUEST``) run synchronously with a
    short timeout and may mutate the payload (injection fields, tools, or
    provider messages). Write-side events (``POST_TOOL``, ``POST_TURN``,
    ``SESSION_END``, ``AFTER_PROVIDER_RESPONSE``) are fire-and-forget; their
    return values are ignored.
    """

    USER_MESSAGE = "user_message"
    PRE_TURN = "pre_turn"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    POST_TURN = "post_turn"
    SESSION_END = "session_end"
    TOOL_DEFINITION = "tool.definition"
    BEFORE_PROVIDER_REQUEST = "before_provider_request"
    AFTER_PROVIDER_RESPONSE = "after_provider_response"


@dataclass
class HookPayload:
    """Per-fire payload passed to every hook for one event.

    The payload is **mutable**: hooks may assign ``inject_text`` or extend
    ``inject_memory_lines`` to surface context to the loop. The loop reads
    those fields after ``fire()`` returns.

    Event-specific fields (``user_message``, ``tool_*``, ``provider_messages``,
    ``tools``, response fields) are populated by the caller based on which
    event is firing; hooks for an unrelated event should ignore them.

    ``TOOL_DEFINITION`` / ``BEFORE_PROVIDER_REQUEST`` may replace ``tools``
    and/or ``provider_messages`` (``Message`` is frozen — replace list entries,
    do not assign to message fields).
    """

    event: HookEvent
    thread_id: str
    request_id: str
    ctx: "TurnContext"

    user_message: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    tool_error: str | None = None

    # TOOL_DEFINITION / BEFORE_PROVIDER_REQUEST (mutable; replace list to filter)
    tools: list["ToolDef"] | None = None
    provider_messages: list["Message"] | None = None
    inner_turn: int | None = None

    # AFTER_PROVIDER_RESPONSE (observational)
    assistant_text: str | None = None
    thinking_text: str | None = None
    tool_requests: list[dict[str, Any]] | None = None
    usage: dict[str, int] | None = None
    provider_error: str | None = None

    inject_text: str | None = None
    inject_memory_lines: list[str] = field(default_factory=list)


HookFn = Callable[[HookPayload], Awaitable[None]]


_in_hook: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "monkeybot_in_hook", default=False
)


_DEFAULT_TIMEOUT_S = 2.0


class HookManager:
    """Register and dispatch hooks across :class:`HookEvent` points.

    One ``HookManager`` is constructed per agent (gateway-owned), shared by
    the loop and any subscribers (e.g. the memory hook). Subagents receive a
    no-op manager so duplicate writes do not race the parent's hooks.

    Fire-and-forget handlers (``timeout_s == 0``) are tracked so
    :meth:`drain_settlement` can wait for them before ``TurnComplete`` or the
    next provider call — without requiring SSE client ACKs.
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookFn]] = {}
        self._pending: set[asyncio.Task[None]] = set()

    def register(self, event: HookEvent, fn: HookFn) -> None:
        """Append ``fn`` to the handler list for ``event``.

        Handlers fire in registration order. Duplicates are allowed; callers
        must not register the same function twice unless duplicate firing is
        intended.
        """
        self._handlers.setdefault(event, []).append(fn)

    def clear(self, event: HookEvent | None = None) -> None:
        """Remove all handlers for ``event`` (or every event when ``None``)."""
        if event is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event, None)

    async def fire(
        self,
        payload: HookPayload,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> HookPayload:
        """Run every handler registered for ``payload.event`` with bounded time.

        ``timeout_s``:
            * ``> 0``: each handler is awaited up to that many seconds; on
              timeout the handler is cancelled and a warning logged.
            * ``== 0``: the handler is scheduled as a background task; this
              method returns immediately without awaiting completion. Use for
              write-side events where the agent should not wait on memory.
              Detached tasks may not finish before the process exits; in
              short-lived handlers (Lambda, Cloud Functions) pass
              ``hook_manager=None`` or use ``timeout_s > 0`` so hooks complete
              before returning. Call :meth:`drain_settlement` before idle
              boundaries when side effects must land.

        Hooks may not recursively trigger this method; nested calls return
        ``payload`` unchanged (after a single debug log line).
        """
        if _in_hook.get():
            _log.debug("hook re-entrancy blocked for event=%s", payload.event)
            return payload

        handlers = self._handlers.get(payload.event)
        if not handlers:
            return payload

        if timeout_s == 0:
            for fn in handlers:
                self._schedule_background(fn, payload)
            return payload

        token = _in_hook.set(True)
        try:
            for fn in handlers:
                await self._run_one(fn, payload, timeout_s)
        finally:
            _in_hook.reset(token)
        return payload

    async def drain_settlement(self, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        """Await outstanding fire-and-forget hook tasks up to ``timeout_s``.

        Does not cancel slow hooks (they may still finish after the barrier).
        Never raises for hook failures — only logs a warning on timeout.
        Safe to call when no tasks are pending.
        """
        pending = [t for t in self._pending if not t.done()]
        if not pending:
            return
        _done, still = await asyncio.wait(pending, timeout=max(0.0, timeout_s))
        if still:
            _log.warning(
                "hook settlement timed out pending=%d timeout_s=%.2f",
                len(still),
                timeout_s,
            )

    @staticmethod
    async def _run_one(fn: HookFn, payload: HookPayload, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(fn(payload), timeout=timeout_s)
        except asyncio.TimeoutError:
            _log.warning(
                "hook timed out event=%s fn=%s timeout_s=%.2f",
                payload.event,
                getattr(fn, "__qualname__", repr(fn)),
                timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning(
                "hook error event=%s fn=%s err=%r",
                payload.event,
                getattr(fn, "__qualname__", repr(fn)),
                exc,
            )

    def _schedule_background(self, fn: HookFn, payload: HookPayload) -> None:
        async def _wrap() -> None:
            token = _in_hook.set(True)
            try:
                await fn(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(
                    "background hook error event=%s fn=%s err=%r",
                    payload.event,
                    getattr(fn, "__qualname__", repr(fn)),
                    exc,
                )
            finally:
                _in_hook.reset(token)

        task = asyncio.create_task(_wrap())
        self._pending.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._pending.discard(t)

        task.add_done_callback(_done)


__all__ = ["HookEvent", "HookPayload", "HookFn", "HookManager"]
