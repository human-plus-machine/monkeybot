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
            "description": (
                "add one or more pending items, mark one done, or remove one by id."
            ),
        },
        "text": {
            "description": (
                "Item text for add: a single string, or a list of strings to append "
                "atomically in one call (preferred for bulk queues)."
            ),
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "minItems": 1},
            ],
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
                "Use add / complete / remove. For add, pass text as a string or a "
                "list of strings to append many items in one call. The live list "
                "appears under ## Todo list in the system prompt when non-empty — "
                "keep it updated as you work."
            ),
            _TODO_LIST_SCHEMA,
        )

    async def execute(self, args: dict[str, object]) -> str:
        action = str(args.get("action") or "").strip().lower()
        if action == "add":
            added_or_err = await self._store.add_many(self._parse_add_texts(args.get("text")))
            if isinstance(added_or_err, str):
                return self._err(added_or_err)
            added = [asdict(item) for item in added_or_err]
            # `item` only for single-add back-compat; bulk callers use `added` + `items`.
            return self._ok(
                action=action,
                item=added[0] if len(added) == 1 else None,
                added=added,
            )
        if action == "complete":
            completed = await self._store.complete(str(args.get("id") or ""))
            if isinstance(completed, str):
                return self._err(completed)
            return self._ok(action=action, item=asdict(completed))
        if action == "remove":
            removed = await self._store.remove(str(args.get("id") or ""))
            if isinstance(removed, str):
                return self._err(removed)
            return self._ok(action=action, item=asdict(removed))
        return self._err(
            "todo_list action must be one of: add, complete, remove.",
            error_kind="validation",
        )

    def _parse_add_texts(self, raw: object) -> list[str]:
        """Normalize ``text`` to a list; store validates emptiness / content."""
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if raw is None:
            return []
        return [str(raw)]

    def _ok(
        self,
        *,
        action: str,
        item: dict[str, str] | None = None,
        added: list[dict[str, str]] | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "ok": True,
            "action": action,
            "items": self._store.snapshot(),
        }
        if item is not None:
            payload["item"] = item
        if added is not None:
            payload["added"] = added
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
                "hint": (
                    "Pass action=add with text (string or list of strings), "
                    "or action=complete|remove with id."
                ),
                "items": self._store.snapshot(),
            },
            ensure_ascii=False,
        )


__all__ = ["TodoListTool"]
