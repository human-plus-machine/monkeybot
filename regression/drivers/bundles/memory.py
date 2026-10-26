"""Outbox drain, row count, in-memory search, and shared-outbox boot.

The turn loop wakes the writer on POST_TURN and never fires SESSION_END, so a
later user message would recall before the flush. This hook awaits the drain
during settlement. Search against an ``InMemoryPalace`` cannot go through the
``mempalace`` CLI; the real executor still owns every other command.
"""

from __future__ import annotations

import json
import os
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.memory.ids import conversation_wing
from monkeybot.core.memory.ingest import workspace_id_from_env
from monkeybot.core.memory.palace import CONVERSATION_ROOM
from monkeybot.core.memory.subsystem import MemoryConfigurationError, MemorySubsystem
from monkeybot.core.tools.types import ToolExecutionResult
from regression.drivers.bundles.protocol import Bundle, RunCtx
from regression.drivers.support.executor import _ObservedExecutor
from testkit.palace import InMemoryPalace

_SHARED_SCHEMES = ("postgresql://", "postgres://", "firestore://")


class _SharedOutbox:
    """Stands in for Postgres or Firestore. ``shares_outbox`` is the boot check.

    ``ensure_ready`` raises before it opens the outbox when the palace is ephemeral.
    """

    shares_outbox = True

    def outbox(self) -> Any:
        raise RuntimeError("shared outbox stub is not a live database")


def _shared_db_url(ctx: RunCtx) -> str | None:
    raw = ctx.env.get("DB_URL", "").strip()
    if not raw:
        paths = ctx.merged.get("paths")
        if isinstance(paths, dict) and isinstance(paths.get("db_url"), str):
            raw = paths["db_url"].strip()
    lowered = raw.lower()
    if any(lowered.startswith(scheme) for scheme in _SHARED_SCHEMES):
        return raw
    return None


def _is_mempalace_search(call: ToolCall) -> bool:
    if call.name != "run_command":
        return False
    argv = call.args.get("argv")
    if not isinstance(argv, list) or len(argv) < 2:
        return False
    return str(argv[0]) == "mempalace" and str(argv[1]) == "search"


class _PalaceSearchExecutor(_ObservedExecutor):
    """Answer ``mempalace search`` from the in-memory palace. Delegate the rest."""

    def __init__(self, observed: _ObservedExecutor, memory: Any) -> None:
        super().__init__(observed._inner)
        self._memory = memory

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        if not _is_mempalace_search(call):
            return await super().execute(call=call, ctx=ctx)
        self.calls.append(call)
        argv = call.args.get("argv")
        query = ""
        if isinstance(argv, list):
            query = " ".join(str(part) for part in argv[2:]).strip().lower()
        wing = conversation_wing(workspace_id_from_env())
        drawers = await self._memory.recall(
            wing=wing,
            room=CONVERSATION_ROOM,
            thread_id=ctx.thread_id,
        )
        if query:
            drawers = [drawer for drawer in drawers if query in drawer.content.lower()]
        stdout = "\n".join(drawer.content for drawer in drawers)
        payload = json.dumps(
            {"ok": True, "stdout": stdout, "stderr": "", "exit_code": 0},
            separators=(",", ":"),
        )
        return ToolExecutionResult.ok_text(payload)


async def _outbox_rows(memory: Any) -> int:
    store = getattr(memory, "_outbox", None)
    conn = getattr(store, "_conn", None)
    if conn is None:
        return 0
    try:
        cursor = await conn.execute("SELECT COUNT(*) FROM memory_outbox")
        row = await cursor.fetchone()
        await cursor.close()
    except Exception:
        return 0
    if row is None:
        return 0
    return int(row[0])


class _MemoryBundle(Bundle):
    order = 41
    hooks = ()

    async def prepare(self, ctx: RunCtx) -> None:
        await _reject_shared_ephemeral(ctx)
        if ctx.boot_error is not None:
            return
        memory = ctx.hook_memory
        executor = ctx.executor
        palace = getattr(memory, "_palace", None)
        if (
            memory is None
            or not isinstance(executor, _ObservedExecutor)
            or not isinstance(palace, InMemoryPalace)
        ):
            return
        ctx.executor = _PalaceSearchExecutor(executor, memory)

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        memory = ctx.hook_memory
        if memory is None or ctx.boot_error is not None:
            return

        async def _drain(payload: object) -> None:
            del payload
            try:
                await memory.drain_writer(timeout_s=5.0)
            except Exception:
                ctx.outbox_count = 0
                return
            ctx.outbox_count = await _outbox_rows(memory)

        manager.register(HookEvent.POST_TURN, _drain)

    async def close(self, ctx: RunCtx) -> None:
        del ctx


async def _reject_shared_ephemeral(ctx: RunCtx) -> None:
    """Postgres/Firestore plus a temp palace raises at boot, before the turn."""
    url = _shared_db_url(ctx)
    if url is None or ctx.hook_memory is None:
        return
    await ctx.hook_memory.close()
    palace = InMemoryPalace(ctx.workdir / "shared-palace", agent_name="loop-scenario")
    memory = MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=url,
        agent_id="loop-scenario",
        agent_name="loop-scenario",
        palace=palace,
        storage=_SharedOutbox(),
        writer_enabled=True,
    )
    ctx.hook_memory = memory
    held = os.environ.pop("MONKEYBOT_MEMORY_ALLOW_EPHEMERAL", None)
    try:
        await memory.ensure_ready()
    except MemoryConfigurationError as exc:
        ctx.boot_error = f"MemoryConfigurationError: {exc}"
    finally:
        if held is not None:
            os.environ["MONKEYBOT_MEMORY_ALLOW_EPHEMERAL"] = held


BUNDLE = _MemoryBundle()
