"""Firestore scheduled-loop store is not implemented yet."""

from __future__ import annotations

from monkeybot.core.persistence.scheduled_loops import (
    ScheduledLoopCreate,
    ScheduledLoopRow,
)


class FirestoreScheduledLoopStore:
    """Placeholder until Firestore scheduled-loop persistence is implemented."""

    _MSG = (
        "Scheduled loops require sqlite:// or postgresql:// DB_URL; "
        "firestore:// is not supported yet."
    )

    async def create(self, spec: ScheduledLoopCreate) -> ScheduledLoopRow:
        raise RuntimeError(self._MSG)

    async def get(self, loop_id: str) -> ScheduledLoopRow | None:
        raise RuntimeError(self._MSG)

    async def list_all(self) -> list[ScheduledLoopRow]:
        raise RuntimeError(self._MSG)

    async def list_due(self, now_ms: int) -> list[ScheduledLoopRow]:
        raise RuntimeError(self._MSG)

    async def claim_tick(self, loop_id: str, worker_id: str) -> ScheduledLoopRow | None:
        raise RuntimeError(self._MSG)

    async def release_stale_claims(self, stale_after_ms: int) -> int:
        raise RuntimeError(self._MSG)

    async def complete_tick(
        self,
        loop_id: str,
        *,
        worker_id: str,
        error: str | None = None,
    ) -> ScheduledLoopRow | None:
        raise RuntimeError(self._MSG)

    async def defer_tick(self, loop_id: str, *, worker_id: str, reason: str) -> None:
        raise RuntimeError(self._MSG)

    async def pause(self, loop_id: str) -> bool:
        raise RuntimeError(self._MSG)

    async def resume(self, loop_id: str) -> bool:
        raise RuntimeError(self._MSG)

    async def stop(self, loop_id: str, *, stop_reason: str = "manual") -> bool:
        raise RuntimeError(self._MSG)
