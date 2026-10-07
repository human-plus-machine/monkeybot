"""Carry verifier state (goal ledger, progress tracker) across history rewrites."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.tracker import ProgressTracker


@dataclass(frozen=True)
class VerifierRewriteEffects:
    """``RewriteEffects`` for the gateway's live ledger and tracker (either may be off)."""

    ledger: GoalLedger | None
    tracker: ProgressTracker | None

    async def branched(
        self, source_thread: str, target_thread: str, dropped_row_ids: Collection[str]
    ) -> None:
        if self.tracker is not None:
            self.tracker.fork_thread(source_thread, target_thread)
        if self.ledger is not None:
            await self.ledger.copy_branch(source_thread, target_thread, dropped_row_ids)

    async def truncated(self, thread_id: str, dropped_row_ids: Collection[str]) -> None:
        if self.tracker is not None:
            self.tracker.reset_conversation_state(thread_id)
        if self.ledger is not None:
            await self.ledger.drop_rows(thread_id, dropped_row_ids)

    async def purged(self, thread_ids: Collection[str]) -> None:
        for thread_id in thread_ids:
            if self.tracker is not None:
                self.tracker.forget(thread_id)
            if self.ledger is not None:
                await self.ledger.clear_thread(thread_id)
