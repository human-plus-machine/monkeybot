"""Process-local palace used by tests (no embedder, no Chroma)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from monkeybot.core.memory.ids import utc_now_iso
from monkeybot.core.memory.palace import (
    CONVERSATION_ROOM,
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDER_IDENTITY_FILE,
    DrawerRecord,
    default_identity_text,
)


class InMemoryPalace:
    """Process-local palace used by tests (no embedder, no Chroma)."""

    def __init__(
        self,
        palace_path: Path,
        *,
        agent_name: str = "test-agent",
        backend: str = "memory",
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.palace_path = Path(palace_path)
        self.backend = backend
        self.embedding_model = embedding_model
        self._agent_name = agent_name
        self._lock = threading.Lock()
        self._drawers: dict[str, DrawerRecord] = {}
        self.palace_path.mkdir(parents=True, exist_ok=True)
        identity = self.palace_path / "identity.txt"
        if not identity.is_file():
            identity.write_text(default_identity_text(agent_name), encoding="utf-8")
        (self.palace_path / EMBEDDER_IDENTITY_FILE).write_text(embedding_model, encoding="utf-8")

    def ensure_ready(self) -> None:
        return

    @contextmanager
    def acquire_write_lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def upsert_drawer(self, drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        filed_at = metadata.get("filed_at") or metadata.get("source_timestamp") or utc_now_iso()
        record = DrawerRecord(
            drawer_id=drawer_id,
            content=content,
            wing=metadata.get("wing") or "main",
            room=metadata.get("room") or CONVERSATION_ROOM,
            filed_at=filed_at,
            metadata=dict(metadata),
        )
        self._drawers[drawer_id] = record

    def get_drawer(self, drawer_id: str) -> DrawerRecord | None:
        return self._drawers.get(drawer_id)

    def wake_up(self, wing: str | None = None) -> str:
        identity = (self.palace_path / "identity.txt").read_text(encoding="utf-8").strip()
        drawers = list(self._drawers.values())
        if wing:
            drawers = [d for d in drawers if d.wing == wing]
        drawers.sort(key=lambda d: d.filed_at, reverse=True)
        lines = [identity, "", "## L1 — ESSENTIAL STORY"]
        if not drawers:
            lines.append("No memories yet.")
            return "\n".join(lines)
        for drawer in drawers[:15]:
            snippet = " ".join(drawer.content.split())
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            lines.append(f"- [{drawer.room}] {snippet}")
        return "\n".join(lines)

    def recall(
        self,
        *,
        wing: str,
        room: str,
        n_results: int = 10,
        thread_id: str | None = None,
    ) -> list[DrawerRecord]:
        matched = [
            d
            for d in self._drawers.values()
            if d.wing == wing
            and d.room == room
            and (thread_id is None or d.metadata.get("thread_id") == thread_id)
        ]
        matched.sort(key=lambda d: d.filed_at, reverse=True)
        return matched[: max(0, n_results)]

    def status(self) -> dict[str, Any]:
        return {
            "palace_path": str(self.palace_path),
            "total_drawers": len(self._drawers),
            "backend": self.backend,
        }
