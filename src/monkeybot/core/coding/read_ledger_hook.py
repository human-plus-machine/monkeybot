"""Forget which files were read once compaction summarizes their contents away."""

from __future__ import annotations

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload


class ReadLedgerCompactionHook:
    """``HISTORY_COMPACTED``: clear the session's read ledger.

    After compaction the model no longer sees the file contents it read earlier, so an
    edit made "from memory" would be blind. Clearing the reads turns that into a
    ``read_before_write`` refusal and a fresh ``read_file``. The ledger's ``written``
    record and command log survive, so the workspace trailer stays accurate.

    The loop also clears in ``_fire_history_compacted`` before this hook runs, so a
    subagent (no hook manager) cannot keep stale reads. This hook remains for the
    same invariant when a caller fires the event without that helper.
    """

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.HISTORY_COMPACTED, self.on_history_compacted)

    async def on_history_compacted(self, payload: HookPayload) -> None:
        ledger = payload.ctx.read_ledger
        if ledger is not None:
            ledger.clear()
