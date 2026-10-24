"""Remind the agent when an open todo list goes stale mid-task.

Counts non-``todo_list`` tool calls per thread since the last ``todo_list``
call. When the session store still has pending or in-progress items and the
count reaches ``stale_after``, one runtime note is injected on ``PRE_TOOL``.
The count resets when ``todo_list`` is called again.

Registered once on the gateway ``HookManager`` in ``rebuild_hooks``. Chat and
the realtime voice loop share that manager, so both see the reminder. The hook
no-ops when the turn has no todo store (the tool is disabled).
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.logging_utils import kv
from monkeybot.todo_list.store import TodoListStore

logger = logging.getLogger(__name__)

_DEFAULT_STALE_AFTER = 8
_THREAD_STATE_CAP = 256
_NOTE = "Todo list unchanged for {count} tool calls: mark finished items done and `start` the next."


class TodoListReminder:
    """``PRE_TOOL`` nudge after a stretch of work that does not touch the list."""

    def __init__(
        self,
        *,
        stale_after: int = _DEFAULT_STALE_AFTER,
        thread_cap: int = _THREAD_STATE_CAP,
    ) -> None:
        self._stale_after = max(1, stale_after)
        self._thread_cap = max(1, thread_cap)
        self._counts: OrderedDict[str, int] = OrderedDict()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.PRE_TOOL, self.on_pre_tool)

    async def on_pre_tool(self, payload: HookPayload) -> None:
        store = payload.ctx.todo_store
        if store is None:
            return
        thread_id = payload.thread_id
        if payload.tool_name == "todo_list":
            self._counts.pop(thread_id, None)
            return
        if not _has_open_items(store):
            self._counts.pop(thread_id, None)
            return
        count = self._bump(thread_id)
        if count != self._stale_after:
            return
        notice = _NOTE.format(count=count)
        logger.info("todo reminder injected %s", kv(thread_id=thread_id, count=count))
        if payload.inject_text:
            payload.inject_text = f"{payload.inject_text.rstrip()}\n\n{notice}"
        else:
            payload.inject_text = notice

    def _bump(self, thread_id: str) -> int:
        count = self._counts.get(thread_id, 0) + 1
        self._counts[thread_id] = count
        self._counts.move_to_end(thread_id)
        while len(self._counts) > self._thread_cap:
            self._counts.popitem(last=False)
        return count


def _has_open_items(store: TodoListStore) -> bool:
    return any(item.status != "done" for item in store.items)


__all__ = ["TodoListReminder"]
