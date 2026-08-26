"""Session-scoped todo list custom tool for the monkeybot harness.

Always on for parent agents unless opted out via ``todo_list.enabled: false``
(or ``MONKEYBOT_TODO_LIST_ENABLED=false``). Not exposed to subagents. Live list
state is injected into the volatile system-prompt tail; ``todos.json`` is mirrored
under the session artifact directory for debugging unless
``todo_list.mirror_to_disk: false``.
"""

from __future__ import annotations

from monkeybot.core.config.snapshot import current_env_flag
from monkeybot.todo_list.store import TodoItem, TodoListStore
from monkeybot.todo_list.tool import TodoListTool

__all__ = [
    "TodoItem",
    "TodoListStore",
    "TodoListTool",
    "todo_list_enabled_from_env",
    "todo_list_mirror_to_disk_from_env",
]


def todo_list_enabled_from_env() -> bool:
    """True unless explicitly opted out (default on).

    Reads ``MONKEYBOT_TODO_LIST_ENABLED`` (mapped from ``todo_list.enabled`` in
    monkeybot.yaml). Recognized off values: ``0``, ``false``, ``no``, ``off``.
    """
    return current_env_flag("MONKEYBOT_TODO_LIST_ENABLED", default=True)


def todo_list_mirror_to_disk_from_env() -> bool:
    """True unless explicitly opted out (default on).

    Reads ``MONKEYBOT_TODO_LIST_MIRROR_TO_DISK`` (mapped from
    ``todo_list.mirror_to_disk`` in monkeybot.yaml). When false, the tool still
    works in memory but never writes ``todos.json`` — useful for read-only or
    ephemeral filesystems. Recognized off values: ``0``, ``false``, ``no``, ``off``.
    """
    return current_env_flag("MONKEYBOT_TODO_LIST_MIRROR_TO_DISK", default=True)
