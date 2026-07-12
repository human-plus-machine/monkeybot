"""Steer and follow-up input queues for mid-turn and idle admission.

Two queues, intentionally separate from HITL ``ToolConfirmationRequest``:

* **Steer** — inject user text at the next safe loop boundary (after the current
  tool batch / before the next provider call) while a turn is in flight.
* **Follow-up** — FIFO prompts drained when the session would otherwise go idle
  (after ``TurnComplete`` / lock release).

Gateway routes enqueue; the agent loop and gateway drain.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass

from monkeybot.core.types.content_blocks import ContentBlock, Text


class AdmissionQueueFullError(Exception):
    """Raised when a steer or follow-up queue is at capacity."""

    def __init__(self, queue: str, max_size: int) -> None:
        super().__init__(f"{queue} queue is full (max {max_size})")
        self.queue = queue
        self.max_size = max_size


def _queue_limit(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class FollowUpItem:
    """One queued prompt waiting for an idle session."""

    request_id: str
    content: list[ContentBlock]


class InputAdmission:
    """Per-session steer + follow-up queues."""

    def __init__(
        self,
        *,
        max_steer: int | None = None,
        max_follow_up: int | None = None,
    ) -> None:
        self.max_steer = (
            max_steer
            if max_steer is not None
            else _queue_limit("MONKEYBOT_STEER_QUEUE_MAX", 8)
        )
        self.max_follow_up = (
            max_follow_up
            if max_follow_up is not None
            else _queue_limit("MONKEYBOT_FOLLOW_UP_QUEUE_MAX", 16)
        )
        self._steer: deque[list[ContentBlock]] = deque()
        self._follow_up: deque[FollowUpItem] = deque()

    @property
    def steer_depth(self) -> int:
        return len(self._steer)

    def enqueue_steer(self, content: list[ContentBlock]) -> int:
        """Append steer content; return 0-based queue position.

        Raises:
            AdmissionQueueFullError: when at capacity.
            ValueError: when ``content`` is empty.
        """
        if not content:
            raise ValueError("steer content must be non-empty")
        if len(self._steer) >= self.max_steer:
            raise AdmissionQueueFullError("steer", self.max_steer)
        self._steer.append(list(content))
        return len(self._steer) - 1

    def enqueue_follow_up(
        self, request_id: str, content: list[ContentBlock]
    ) -> int:
        """Append a follow-up prompt; return 0-based queue position."""
        if not request_id.strip():
            raise ValueError("follow-up request_id must be non-empty")
        if not content:
            raise ValueError("follow-up content must be non-empty")
        if len(self._follow_up) >= self.max_follow_up:
            raise AdmissionQueueFullError("follow_up", self.max_follow_up)
        self._follow_up.append(FollowUpItem(request_id=request_id, content=list(content)))
        return len(self._follow_up) - 1

    def pop_steer(self) -> list[ContentBlock] | None:
        """Take the oldest steer message, or ``None`` if empty."""
        if not self._steer:
            return None
        return self._steer.popleft()

    def pop_follow_up(self) -> FollowUpItem | None:
        """Take the oldest follow-up, or ``None`` if empty."""
        if not self._follow_up:
            return None
        return self._follow_up.popleft()

    def requeue_follow_up_front(self, item: FollowUpItem) -> None:
        """Put a follow-up back at the front (failed lock acquire)."""
        self._follow_up.appendleft(item)

    def clear_steer(self) -> None:
        """Drop pending steer injections (e.g. on cancel)."""
        self._steer.clear()

    def clear_all(self) -> None:
        """Drop steer and follow-up queues (session teardown)."""
        self._steer.clear()
        self._follow_up.clear()


def preview_text(content: list[ContentBlock], *, limit: int = 200) -> str:
    """Short plain-text preview for observability events."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Text) and block.text.strip():
            parts.append(block.text.strip())
    joined = " ".join(parts).strip()
    if len(joined) <= limit:
        return joined
    return joined[: limit - 1] + "…"


__all__ = [
    "AdmissionQueueFullError",
    "FollowUpItem",
    "InputAdmission",
    "preview_text",
]
