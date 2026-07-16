"""CustomTool wrapper for the session-scoped todo list."""

from __future__ import annotations

import json
from dataclasses import asdict

from monkeybot.core.types.types_tools import ToolDef
from monkeybot.todo_list.store import TodoListStore

_TODO_LIST_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "complete", "remove"],
            "description": "add a pending item, mark one done, or remove one by id.",
        },
        "text": {
            "type": "string",
            "description": "Item text (required for add).",
        },
        "id": {
            "type": "string",
            "description": "Item id (required for complete and remove).",
        },
    },
    "required": ["action"],
}


class TodoListTool:
    """Adapts a :class:`TodoListStore` to the :class:`~monkeybot.core.context.CustomTool` protocol."""

    def __init__(self, store: TodoListStore) -> None:
        self._store = store
        self.tool_def = ToolDef(
            "todo_list",
            (
                "Maintain an ordered, session-scoped task list. "
                "Use add / complete / remove. The live list appears under "
                "## Todo list in the system prompt when non-empty — keep it updated as you work."
            ),
            _TODO_LIST_SCHEMA,
        )

    async def execute(self, args: dict[str, object]) -> str:
        action = str(args.get("action") or "").strip().lower()
        if action == "add":
            result = self._store.add(str(args.get("text") or ""))
            if isinstance(result, str):
                return self._err(result)
            return self._ok(action=action, item=asdict(result))
        if action == "complete":
            result = self._store.complete(str(args.get("id") or ""))
            if isinstance(result, str):
                return self._err(result)
            return self._ok(action=action, item=asdict(result))
        if action == "remove":
            result = self._store.remove(str(args.get("id") or ""))
            if isinstance(result, str):
                return self._err(result)
            return self._ok(action=action, item=asdict(result))
        return self._err(
            "todo_list action must be one of: add, complete, remove.",
            error_kind="validation",
        )

    def _ok(self, *, action: str, item: dict[str, str]) -> str:
        payload: dict[str, object] = {
            "ok": True,
            "action": action,
            "item": item,
            "items": self._store.snapshot(),
        }
        if self._store.mirror_error is not None:
            # Debug mirror is stale; the in-memory list (returned above) is still authoritative.
            payload["mirror_warning"] = (
                "todos.json debug mirror write failed; list state is unaffected."
            )
        return json.dumps(payload, ensure_ascii=False)

    def _err(self, message: str, *, error_kind: str = "validation") -> str:
        return json.dumps(
            {
                "ok": False,
                "error_kind": error_kind,
                "message": message,
                "hint": "Pass action=add with text, or action=complete|remove with id.",
                "items": self._store.snapshot(),
            },
            ensure_ascii=False,
        )


__all__ = ["TodoListTool"]
