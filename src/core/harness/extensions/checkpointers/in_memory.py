"""In-memory :class:`Checkpointer` shipped as a builtin backend.

Subclasses the new ABC at :mod:`src.core.harness.extensions.base`. Suitable for
single-process development, tests, and example bots. Payloads are serialized
via :func:`pickle.dumps` so arbitrary state graphs round-trip.
"""

from __future__ import annotations

import asyncio
import itertools
import pickle
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..base import Checkpointer
from ..errors import CheckpointMissing
from ..values import CheckpointRef


@dataclass
class _Entry:
    """Internal store row pairing a :class:`CheckpointRef` with its payload."""

    ref: CheckpointRef
    payload: bytes


class InMemoryCheckpointer(Checkpointer):
    """Process-local checkpointer keyed by ``session_id``.

    Writes are serialized with :func:`pickle.dumps`; the ``CheckpointRef.uri``
    scheme is ``memory:///{session_id}/{checkpoint_id}``. Checkpoint ids are
    monotonic per session (``seq + uuid`` suffix) so contract invariant
    ``CKPT-C-01`` holds even under concurrent writes.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[_Entry]] = {}
        self._counters: dict[str, itertools.count[int]] = {}
        self._lock = asyncio.Lock()

    def _next_id(self, session_id: str) -> str:
        counter = self._counters.setdefault(session_id, itertools.count(1))
        seq = next(counter)
        return f"{seq:016d}-{uuid.uuid4().hex[:8]}"

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        """Persist ``state`` in memory and return a fresh :class:`CheckpointRef`."""
        async with self._lock:
            payload = pickle.dumps(dict(state))
            checkpoint_id = self._next_id(session_id)
            ref = CheckpointRef(
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                reason=reason,
                created_at=datetime.now(UTC),
                bytes=len(payload),
                uri=f"memory:///{session_id}/{checkpoint_id}",
            )
            self._store.setdefault(session_id, []).append(_Entry(ref=ref, payload=payload))
            return ref

    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the stored payload for ``checkpoint_id`` (or the latest)."""
        entries = self._store.get(session_id) or []
        if checkpoint_id is None:
            if not entries:
                return None
            return self._deserialize(entries[-1].payload)
        for entry in entries:
            if entry.ref.checkpoint_id == checkpoint_id:
                return self._deserialize(entry.payload)
        raise CheckpointMissing(session_id, checkpoint_id)

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        """Return checkpoint refs newest-first up to ``limit`` entries."""
        entries = self._store.get(session_id, [])
        return [entry.ref for entry in reversed(entries)][:limit]

    async def delete_session(self, session_id: str) -> None:
        """Remove every checkpoint tied to ``session_id``."""
        self._store.pop(session_id, None)
        self._counters.pop(session_id, None)

    async def gc(self, older_than: timedelta) -> int:
        """Drop entries older than ``now - older_than`` and return the count."""
        threshold = datetime.now(UTC) - older_than
        removed = 0
        async with self._lock:
            for session_id, entries in list(self._store.items()):
                kept = [entry for entry in entries if entry.ref.created_at >= threshold]
                removed += len(entries) - len(kept)
                if kept:
                    self._store[session_id] = kept
                else:
                    self._store.pop(session_id, None)
                    self._counters.pop(session_id, None)
        return removed

    @staticmethod
    def _deserialize(payload: bytes) -> Mapping[str, Any]:
        result: Any = pickle.loads(payload)  # noqa: S301 - trusted in-process payload
        if not isinstance(result, Mapping):
            raise TypeError(
                f"InMemoryCheckpointer expected a Mapping payload, got {type(result).__name__}"
            )
        return result


__all__ = ["InMemoryCheckpointer"]
