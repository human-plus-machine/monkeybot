"""Pending UI responses for tool confirmations, ``ask_user``, and elicitations.

A preset answer is applied when ``register_pending`` runs, which is how the
loop unit tests approve a call before the inspector waits. ``resolve_pending``
answers a call that is already waiting, which is how a scenario step answers
``tool_confirmation`` and ``ask_user`` after the request event.

A preset payload of ``{"_never": True}`` leaves the future pending so a real
timeout can fire. ``{"_timeout": True}`` still resolves immediately; the
inspector treats that payload as the timeout result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal


class PresetPendingBus:
    """Futures for one session's pending UI responses."""

    def __init__(
        self,
        preset: dict[str, Any] | None = None,
        *,
        on_publish: Callable[[str], None] | None = None,
    ) -> None:
        self._preset = dict(preset or {})
        self._deferred: dict[str, Any] = {}
        self._terminated: list[str] = []
        self.pending_responses: dict[str, asyncio.Future[Any]] = {}
        self.published: list[str] = []
        self.on_publish = on_publish

    def register_pending(self, key: str) -> asyncio.Future[Any]:
        """Return a future, already completed when this key was preset or resolved."""
        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[key] = fut
        if key in self._deferred:
            self._complete(key, fut, self._deferred.pop(key))
        elif key in self._preset and not _is_never(self._preset[key]):
            self._complete(key, fut, self._preset[key])
        return fut

    def resolve_pending(self, key: str, payload: Any) -> bool:
        """Complete ``key``. A key that is not registered yet completes on register.

        Returns False when the future was already completed.
        """
        fut = self.pending_responses.get(key)
        if fut is None:
            self._deferred[key] = payload
            return True
        if fut.done():
            return False
        self._complete(key, fut, payload)
        return True

    def is_pending_or_terminal(self, key: str) -> Literal["pending", "terminated", "unknown"]:
        fut = self.pending_responses.get(key)
        if fut is not None and not fut.done():
            return "pending"
        if key in self._terminated:
            return "terminated"
        return "unknown"

    def abandon_pending_timeout(self, key: str) -> None:
        fut = self.pending_responses.pop(key, None)
        if fut is not None and not fut.done():
            fut.cancel()
        self._terminated.append(key)

    def abandon_pending_cancel_all(self) -> None:
        for key in list(self.pending_responses):
            fut = self.pending_responses.pop(key, None)
            if fut is not None and not fut.done():
                fut.cancel()
            self._terminated.append(key)

    async def publish_data(self, data_json: str, *, lane: str = "primary") -> int:
        """Record one JSON event. ``ask_user`` publishes here before it waits."""
        del lane
        self.published.append(data_json)
        if self.on_publish is not None:
            self.on_publish(data_json)
        return len(self.published)

    def _complete(self, key: str, fut: asyncio.Future[Any], payload: Any) -> None:
        if not fut.done():
            fut.set_result(payload)
        self.pending_responses.pop(key, None)
        self._terminated.append(key)


def _is_never(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("_never") is True


__all__ = ["PresetPendingBus"]
