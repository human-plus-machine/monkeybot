"""Session-scoped todo list store with optional debug disk mirror.

Memory is the agent-facing source of truth. After each successful mutation the
store snapshots to ``todos.json`` under the session artifact directory
(``.monkeybot/transcripts/{UTC}_{session_id}/``) for live debugging — same folder
layout as transcripts, written without requiring transcripts to be enabled.

Disk I/O (mutation mirrors and reconnect hydration) runs in
``asyncio.to_thread`` so neither path blocks the gateway event loop. Opt out of
mirroring with ``todo_list.mirror_to_disk: false``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
_ITEM_ID_RE = re.compile(r"^t(\d+)$")


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

    def __init__(
        self,
        session_id: str,
        *,
        workspace_root: Path,
        mirror_to_disk: bool | None = None,
    ) -> None:
        from monkeybot.todo_list import todo_list_mirror_to_disk_from_env

        self.session_id = session_id
        self._workspace_root = workspace_root.resolve()
        self._mirror_to_disk_enabled = (
            todo_list_mirror_to_disk_from_env() if mirror_to_disk is None else mirror_to_disk
        )
        self._items: list[TodoItem] = []
        self._next_n = 1
        self._session_dir: Path | None = None
        self._mirror_error: str | None = None

    async def hydrate_from_disk(self) -> None:
        """Best-effort restore from ``todos.json`` without blocking the event loop.

        Call once after construction when reconnecting or starting a session that
        may already have a debug mirror on disk. No-op when mirroring is disabled.
        """
        if not self._mirror_to_disk_enabled:
            return
        await asyncio.to_thread(self._load_from_disk_sync)

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

    async def add(self, text: str) -> TodoItem | str:
        """Append a pending item. Returns the item or an error message string."""
        result = await self.add_many([text])
        if isinstance(result, str):
            return result
        return result[0]

    async def add_many(self, texts: list[str]) -> list[TodoItem] | str:
        """Append one or more pending items atomically.

        Validates the full batch before mutating. On success returns the new
        items (in order); on failure returns an error string and leaves the
        list unchanged.
        """
        if not texts:
            return "todo_list add requires non-empty text (string or non-empty list)."
        cleaned_batch: list[str] = []
        for raw in texts:
            cleaned = str(raw).strip()
            if not cleaned:
                return "todo_list add requires non-empty text."
            if len(cleaned) > _MAX_TEXT_CHARS:
                return f"todo_list text exceeds {_MAX_TEXT_CHARS} characters."
            cleaned_batch.append(cleaned)
        if len(self._items) + len(cleaned_batch) > _MAX_ITEMS:
            return (
                f"todo_list would exceed max {_MAX_ITEMS} items "
                f"({len(self._items)} existing + {len(cleaned_batch)} new); "
                "complete or remove some first."
            )
        added: list[TodoItem] = []
        for cleaned in cleaned_batch:
            item = TodoItem(id=f"t{self._next_n}", text=cleaned, status="pending")
            self._next_n += 1
            self._items.append(item)
            added.append(item)
        ids = ",".join(item.id for item in added)
        self._log_mutate("add", ids)
        await self._mirror_to_disk()
        return added

    async def complete(self, item_id: str) -> TodoItem | str:
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
        await self._mirror_to_disk()
        return updated

    async def remove(self, item_id: str) -> TodoItem | str:
        """Remove an item. Returns the removed item or an error message string."""
        idx = self._index_of(item_id)
        if idx is None:
            return f"todo_list item id not found: {item_id!r}."
        removed = self._items.pop(idx)
        self._log_mutate("remove", removed.id)
        await self._mirror_to_disk()
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

    def _load_from_disk_sync(self) -> None:
        """Best-effort restore from an existing ``todos.json`` (e.g. after reconnect)."""
        try:
            session_dir = resolve_session_artifact_dir(self._workspace_root, self.session_id)
            path = session_dir / _TODOS_FILENAME
            if not path.is_file():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            items_raw = raw.get("items") if isinstance(raw, dict) else None
            if not isinstance(items_raw, list):
                return
            restored: list[TodoItem] = []
            max_n = 0
            for entry in items_raw:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("id") or "").strip()
                text = str(entry.get("text") or "")
                status = entry.get("status")
                if not item_id or status not in ("pending", "done"):
                    continue
                restored.append(TodoItem(id=item_id, text=text, status=status))
                match = _ITEM_ID_RE.match(item_id)
                if match:
                    max_n = max(max_n, int(match.group(1)))
            self._items = restored
            self._next_n = max_n + 1
            self._session_dir = session_dir
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "todo_list disk restore failed %s",
                kv(session_id=self.session_id),
                exc_info=True,
            )

    def _write_mirror_sync(self) -> None:
        """Synchronous mirror write — always run via ``asyncio.to_thread``."""
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

    async def _mirror_to_disk(self) -> None:
        """Best-effort live snapshot for debugging; never raises to callers.

        Memory (``self._items``) stays authoritative regardless of outcome; a
        failure here only means the ``todos.json`` debug mirror is stale, which
        is recorded on ``self._mirror_error`` for callers to surface.
        """
        if not self._mirror_to_disk_enabled:
            return
        try:
            await asyncio.to_thread(self._write_mirror_sync)
            self._mirror_error = None
        except OSError as exc:
            self._mirror_error = str(exc)
            logger.warning(
                "todo_list disk mirror failed %s",
                kv(session_id=self.session_id),
                exc_info=True,
            )


__all__ = ["TodoItem", "TodoListStore", "TodoStatus"]
