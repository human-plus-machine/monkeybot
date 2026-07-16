"""Session-scoped todo list store with optional debug disk mirror.

Memory is the agent-facing source of truth. After each successful mutation the
store snapshots to ``todos.json`` under the session artifact directory
(``.monkeybot/transcripts/{UTC}_{session_id}/``) for live debugging — same folder
layout as transcripts, written without requiring transcripts to be enabled.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.transcript import now_iso, resolve_session_artifact_dir

logger = logging.getLogger(__name__)

TodoStatus = Literal["pending", "done"]

_MAX_ITEMS = 50
_MAX_TEXT_CHARS = 500
_TODOS_FILENAME = "todos.json"


@dataclass(frozen=True)
class TodoItem:
    """One ordered task in a session todo list."""

    id: str
    text: str
    status: TodoStatus


class TodoListStore:
    """Mutable ordered todo list for one gateway session.

    Process-local only — not shared across gateway replicas.
    """

    def __init__(self, session_id: str, *, workspace_root: Path) -> None:
        self.session_id = session_id
        self._workspace_root = workspace_root.resolve()
        self._items: list[TodoItem] = []
        self._next_n = 1
        self._session_dir: Path | None = None
        self._mirror_error: str | None = None

    @property
    def items(self) -> tuple[TodoItem, ...]:
        return tuple(self._items)

    @property
    def mirror_error(self) -> str | None:
        """Message from the most recent failed disk mirror, or None if the last write succeeded.

        Memory remains authoritative on mirror failure; callers can surface this
        (e.g. in the tool response) so a broken debug mirror isn't purely silent.
        """
        return self._mirror_error

    def snapshot(self) -> list[dict[str, str]]:
        """JSON-serializable copy of the current list."""
        return [asdict(item) for item in self._items]

    def format_lines(self) -> str:
        """Numbered status lines for the volatile prompt, or ``\"\"`` when empty."""
        if not self._items:
            return ""
        return "\n".join(
            f"{i}. [{item.status}] {item.text}" for i, item in enumerate(self._items, start=1)
        )

    def add(self, text: str) -> TodoItem | str:
        """Append a pending item. Returns the item or an error message string."""
        cleaned = text.strip()
        if not cleaned:
            return "todo_list add requires non-empty text."
        if len(cleaned) > _MAX_TEXT_CHARS:
            return f"todo_list text exceeds {_MAX_TEXT_CHARS} characters."
        if len(self._items) >= _MAX_ITEMS:
            return f"todo_list is full (max {_MAX_ITEMS} items); complete or remove some first."
        item = TodoItem(id=f"t{self._next_n}", text=cleaned, status="pending")
        self._next_n += 1
        self._items.append(item)
        self._log_mutate("add", item.id)
        self._mirror_to_disk()
        return item

    def complete(self, item_id: str) -> TodoItem | str:
        """Mark an item done. Returns the item or an error message string."""
        idx = self._index_of(item_id)
        if idx is None:
            return f"todo_list item id not found: {item_id!r}."
        current = self._items[idx]
        if current.status == "done":
            return current
        updated = TodoItem(id=current.id, text=current.text, status="done")
        self._items[idx] = updated
        self._log_mutate("complete", updated.id)
        self._mirror_to_disk()
        return updated

    def remove(self, item_id: str) -> TodoItem | str:
        """Remove an item. Returns the removed item or an error message string."""
        idx = self._index_of(item_id)
        if idx is None:
            return f"todo_list item id not found: {item_id!r}."
        removed = self._items.pop(idx)
        self._log_mutate("remove", removed.id)
        self._mirror_to_disk()
        return removed

    def _log_mutate(self, action: str, item_id: str) -> None:
        logger.debug(
            "todo_list mutated %s",
            kv(
                session_id=self.session_id,
                action=action,
                item_id=item_id,
                count=len(self._items),
            ),
        )

    def _index_of(self, item_id: str) -> int | None:
        key = item_id.strip()
        if not key:
            return None
        for i, item in enumerate(self._items):
            if item.id == key:
                return i
        return None

    def _mirror_to_disk(self) -> None:
        """Best-effort live snapshot for debugging; never raises to callers.

        Memory (``self._items``) stays authoritative regardless of outcome; a
        failure here only means the ``todos.json`` debug mirror is stale, which
        is recorded on ``self._mirror_error`` for callers to surface.
        """
        try:
            if self._session_dir is None:
                self._session_dir = resolve_session_artifact_dir(
                    self._workspace_root, self.session_id
                )
            path = self._session_dir / _TODOS_FILENAME
            payload = {
                "session_id": self.session_id,
                "updated_at": now_iso(),
                "items": self.snapshot(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._mirror_error = None
        except OSError as exc:
            self._mirror_error = str(exc)
            logger.warning(
                "todo_list disk mirror failed %s",
                kv(session_id=self.session_id),
                exc_info=True,
            )


__all__ = ["TodoItem", "TodoListStore", "TodoStatus"]
