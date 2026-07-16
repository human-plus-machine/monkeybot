"""Session-scoped todo list custom tool for the monkeybot harness.

Always on for parent agents unless opted out via ``todo_list.enabled: false``
(or ``MONKEYBOT_TODO_LIST_ENABLED=false``). Not exposed to subagents. Live list
state is injected into the volatile system-prompt tail; ``todos.json`` is mirrored
under the session artifact directory for debugging.
"""

from __future__ import annotations

import os

from monkeybot.todo_list.store import TodoItem, TodoListStore
from monkeybot.todo_list.tool import TodoListTool

__all__ = [
    "TodoItem",
    "TodoListStore",
    "TodoListTool",
    "todo_list_enabled_from_env",
]


def todo_list_enabled_from_env() -> bool:
    """True unless explicitly opted out (default on).

    Reads ``MONKEYBOT_TODO_LIST_ENABLED`` (mapped from ``todo_list.enabled`` in
    monkeybot.yaml). Recognized off values: ``0``, ``false``, ``no``, ``off``.
    """
    raw = os.environ.get("MONKEYBOT_TODO_LIST_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}
