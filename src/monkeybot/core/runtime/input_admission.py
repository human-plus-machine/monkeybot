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


class FollowUpNotFoundError(Exception):
    """Raised when a follow-up ``request_id`` is not in the queue."""

    def __init__(self, request_id: str) -> None:
        super().__init__(f"follow-up {request_id} is not queued")
        self.request_id = request_id


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
    """One queued prompt waiting for an idle session.

    ``first_lock_fail_at_ms`` is set when a drain attempt fails to acquire the
    durable session turn lock; used to bound how long we retry before dropping.
    """

    request_id: str
    content: list[ContentBlock]
    first_lock_fail_at_ms: int | None = None


@dataclass(frozen=True)
class SteerItem:
    """One mid-turn steer. Provenance is tagged at enqueue, never inferred."""

    content: list[ContentBlock]
    provenance: str = "human"
    queued_request_id: str | None = None


class InputAdmission:
    """Per-session steer + follow-up queues."""

    def __init__(
        self,
        *,
        max_steer: int | None = None,
        max_follow_up: int | None = None,
    ) -> None:
        self.max_steer = (
            max_steer if max_steer is not None else _queue_limit("MONKEYBOT_STEER_QUEUE_MAX", 8)
        )
        self.max_follow_up = (
            max_follow_up
            if max_follow_up is not None
            else _queue_limit("MONKEYBOT_FOLLOW_UP_QUEUE_MAX", 16)
        )
        self._steer: deque[SteerItem] = deque()
        self._follow_up: deque[FollowUpItem] = deque()
        # Ids popped by drain and not yet committed or requeued. DELETE of one
        # of these succeeds and marks the item so the drain path does not put
        # it back or start a turn after telling the client it was gone.
        self._draining_ids: set[str] = set()
        self._dropped_while_draining: set[str] = set()

    @property
    def steer_depth(self) -> int:
        return len(self._steer)

    @property
    def follow_up_depth(self) -> int:
        return len(self._follow_up)

    def enqueue_steer(
        self,
        content: list[ContentBlock],
        *,
        provenance: str = "human",
        queued_request_id: str | None = None,
    ) -> int:
        """Append steer content; return 0-based queue position.

        Raises:
            AdmissionQueueFullError: when at capacity.
            ValueError: when ``content`` is empty.
        """
        if not content:
            raise ValueError("steer content must be non-empty")
        if len(self._steer) >= self.max_steer:
            raise AdmissionQueueFullError("steer", self.max_steer)
        self._steer.append(
            SteerItem(
                content=list(content),
                provenance=provenance,
                queued_request_id=queued_request_id,
            )
        )
        return len(self._steer) - 1

    def enqueue_follow_up(self, request_id: str, content: list[ContentBlock]) -> int:
        """Append a follow-up prompt; return 0-based queue position."""
        if not request_id.strip():
            raise ValueError("follow-up request_id must be non-empty")
        if not content:
            raise ValueError("follow-up content must be non-empty")
        if len(self._follow_up) >= self.max_follow_up:
            raise AdmissionQueueFullError("follow_up", self.max_follow_up)
        self._follow_up.append(FollowUpItem(request_id=request_id, content=list(content)))
        return len(self._follow_up) - 1

    def _follow_up_index(self, request_id: str) -> int | None:
        return next(
            (i for i, item in enumerate(self._follow_up) if item.request_id == request_id),
            None,
        )

    def promote_follow_up(self, request_id: str) -> int:
        """Move a follow-up into the steer queue. Return 0-based steer position.

        Capacity is checked before removal so a full steer queue cannot drop
        the follow-up. Remaining follow-ups keep their FIFO order.

        Raises:
            FollowUpNotFoundError: when ``request_id`` is not queued.
            AdmissionQueueFullError: when steer is at capacity (item stays queued).
            ValueError: when ``request_id`` is empty.
        """
        if not request_id.strip():
            raise ValueError("follow-up request_id must be non-empty")
        if len(self._steer) >= self.max_steer:
            raise AdmissionQueueFullError("steer", self.max_steer)
        index = self._follow_up_index(request_id)
        if index is None:
            raise FollowUpNotFoundError(request_id)
        item = self._follow_up[index]
        del self._follow_up[index]
        self._steer.append(
            SteerItem(
                content=list(item.content),
                provenance="human",
                queued_request_id=item.request_id,
            )
        )
        return len(self._steer) - 1

    def drop_follow_up(self, request_id: str) -> None:
        """Remove a follow-up without promoting it.

        If drain has popped the item and is awaiting the turn lock, this still
        succeeds and marks the id so ``requeue_follow_up_front`` /
        ``finish_follow_up_drain`` will not start it.

        Raises:
            FollowUpNotFoundError: when ``request_id`` is not queued or draining.
            ValueError: when ``request_id`` is empty.
        """
        if not request_id.strip():
            raise ValueError("follow-up request_id must be non-empty")
        index = self._follow_up_index(request_id)
        if index is not None:
            del self._follow_up[index]
            return
        if request_id in self._draining_ids:
            self._dropped_while_draining.add(request_id)
            return
        raise FollowUpNotFoundError(request_id)

    def pop_steer(self) -> SteerItem | None:
        """Take the oldest steer message, or ``None`` if empty."""
        if not self._steer:
            return None
        return self._steer.popleft()

    def pop_follow_up(self) -> FollowUpItem | None:
        """Take the oldest follow-up, or ``None`` if empty."""
        if not self._follow_up:
            return None
        item = self._follow_up.popleft()
        self._draining_ids.add(item.request_id)
        return item

    def requeue_follow_up_front(self, item: FollowUpItem) -> bool:
        """Put a follow-up back at the front (failed lock acquire).

        Returns False when DELETE won while this item was popped for drain;
        the caller must not retry it.
        """
        self._draining_ids.discard(item.request_id)
        if item.request_id in self._dropped_while_draining:
            self._dropped_while_draining.discard(item.request_id)
            return False
        self._follow_up.appendleft(item)
        return True

    def finish_follow_up_drain(self, request_id: str) -> bool:
        """Commit a successful drain. Returns False if DELETE won the race."""
        self._draining_ids.discard(request_id)
        if request_id in self._dropped_while_draining:
            self._dropped_while_draining.discard(request_id)
            return False
        return True

    def restore_promoted_steers(self) -> int:
        """Move leftover promoted steers back onto the front of the follow-up FIFO.

        Steers are only drained at the top of each inner turn, so a promote
        during the final inner turn would otherwise sit in ``_steer`` until a
        later unrelated turn (or be deleted by cancel). Restoring them here
        makes them drain as their own turns, matching pre-promote follow-up
        semantics. Regular (non-promoted) steers stay in the steer queue.

        Capacity is not enforced: these items were already admitted.
        Returns how many items were restored.
        """
        remaining: deque[SteerItem] = deque()
        promoted: list[FollowUpItem] = []
        while self._steer:
            item = self._steer.popleft()
            if item.queued_request_id:
                promoted.append(
                    FollowUpItem(
                        request_id=item.queued_request_id,
                        content=list(item.content),
                    )
                )
            else:
                remaining.append(item)
        self._steer = remaining
        for follow_up in reversed(promoted):
            self._follow_up.appendleft(follow_up)
        return len(promoted)

    def clear_steer(self) -> None:
        """Drop pending steer injections (e.g. on cancel).

        Promoted follow-ups are restored to the follow-up queue first so they
        still drain after the cancelled turn.
        """
        self.restore_promoted_steers()
        self._steer.clear()

    def clear_all(self) -> None:
        """Drop steer and follow-up queues (session teardown)."""
        self._steer.clear()
        self._follow_up.clear()
        self._draining_ids.clear()
        self._dropped_while_draining.clear()


def join_text(content: list[ContentBlock]) -> str:
    """Full plain-text of ``Text`` blocks. Used by the goal ledger (verbatim)."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Text) and block.text.strip():
            parts.append(block.text.strip())
    return " ".join(parts).strip()


def preview_text(content: list[ContentBlock], *, limit: int = 200) -> str:
    """Short plain-text preview for observability events."""
    joined = join_text(content)
    if len(joined) <= limit:
        return joined
    return joined[: limit - 1] + "…"


__all__ = [
    "AdmissionQueueFullError",
    "FollowUpItem",
    "FollowUpNotFoundError",
    "InputAdmission",
    "SteerItem",
    "join_text",
    "preview_text",
]
