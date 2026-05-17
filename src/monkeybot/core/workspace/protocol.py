"""Pluggable workspace storage for durable markdown memory (local FS, GCS, S3)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkspaceStorage(Protocol):
    """Async key-value file storage under a logical root (memory tree).

    Paths are always POSIX-style relative keys (e.g. ``raw/foo.md``, ``INDEX.md``).
    Implementations must not require a running event loop at import time.
    """

    async def read_text(self, path: str) -> str:
        """Read UTF-8 text; raise ``FileNotFoundError`` if missing."""

    async def write_text(self, path: str, content: str) -> None:
        """Write or replace file at ``path`` (parent prefixes created as needed)."""

    async def append_text(self, path: str, content: str) -> None:
        """Append ``content`` to ``path`` (create file if absent)."""

    async def exists(self, path: str) -> bool:
        """Return whether ``path`` exists as a file."""

    async def list_files(self, prefix: str = "") -> list[str]:
        """List all file paths under ``prefix``, recursive, POSIX relative paths."""

    async def delete(self, path: str) -> None:
        """Delete file at ``path`` if it exists (ignore missing)."""

    async def move(self, src: str, dest: str) -> None:
        """Move/rename ``src`` to ``dest`` (atomic when the backend allows)."""

    async def gc_prefix(self, prefix: str, max_age_sec: float) -> dict[str, int]:
        """Best-effort GC of files under ``prefix`` older than ``max_age_sec``.

        Returns ``{"scanned": int, "deleted": int, "errors": int}``.
        Cloud backends may return zeros and rely on bucket lifecycle rules.
        """


__all__ = ["WorkspaceStorage"]
