"""Local filesystem :class:`WorkspaceStorage` (zero extra dependencies)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from monkeybot.core.workspace.protocol import WorkspaceStorage

_log = logging.getLogger(__name__)


def _posix_rel(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    return rel.as_posix()


class LocalWorkspaceStorage:
    """``pathlib``-backed storage; blocking I/O runs in ``asyncio.to_thread``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _abs(self, path: str) -> Path:
        key = path.strip().replace("\\", "/").lstrip("/")
        return (self._root / key).resolve()

    def _ensure_under_root(self, path: Path) -> None:
        path.relative_to(self._root)

    async def read_text(self, path: str) -> str:
        p = self._abs(path)
        self._ensure_under_root(p)

        def _read() -> str:
            if not p.is_file():
                raise FileNotFoundError(str(p))
            return p.read_text(encoding="utf-8")

        return await asyncio.to_thread(_read)

    async def write_text(self, path: str, content: str) -> None:
        p = self._abs(path)
        self._ensure_under_root(p)

        def _write() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def append_text(self, path: str, content: str) -> None:
        p = self._abs(path)
        self._ensure_under_root(p)

        def _append() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(content)

        await asyncio.to_thread(_append)

    async def exists(self, path: str) -> bool:
        p = self._abs(path)
        self._ensure_under_root(p)

        def _exists() -> bool:
            return p.is_file()

        return await asyncio.to_thread(_exists)

    async def list_files(self, prefix: str = "") -> list[str]:
        root = self._root
        pre = prefix.strip().replace("\\", "/")
        if pre and not pre.endswith("/"):
            pre = pre + "/"

        def _list() -> list[str]:
            base = root if not pre else (root / pre).resolve()
            if not base.exists():
                return []
            self._ensure_under_root(base)
            out: list[str] = []
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    rel = _posix_rel(root, path)
                except ValueError:
                    continue
                out.append(rel)
            return out

        return await asyncio.to_thread(_list)

    async def delete(self, path: str) -> None:
        p = self._abs(path)
        self._ensure_under_root(p)

        def _unlink() -> None:
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("delete failed for %s: %r", p, exc)

        await asyncio.to_thread(_unlink)

    async def move(self, src: str, dest: str) -> None:
        sp = self._abs(src)
        dp = self._abs(dest)
        self._ensure_under_root(sp)
        self._ensure_under_root(dp)

        def _mv() -> None:
            dp.parent.mkdir(parents=True, exist_ok=True)
            try:
                sp.replace(dp)
            except OSError:
                shutil.move(str(sp), str(dp))

        await asyncio.to_thread(_mv)

    async def mtime(self, path: str) -> float | None:
        p = self._abs(path)
        self._ensure_under_root(p)

        def _mtime() -> float | None:
            if not p.is_file():
                return None
            return float(p.stat().st_mtime)

        return await asyncio.to_thread(_mtime)

    async def gc_prefix(self, prefix: str, max_age_sec: float) -> dict[str, int]:
        root = self._root
        pre = prefix.strip().replace("\\", "/")
        if pre and not pre.endswith("/"):
            pre = pre + "/"
        cutoff = time.time() - float(max_age_sec)

        def _sweep() -> dict[str, int]:
            counts = {"scanned": 0, "deleted": 0, "errors": 0}
            base = root if not pre else (root / pre).resolve()
            if not base.exists():
                return counts
            try:
                base.relative_to(root.resolve())
            except ValueError:
                return counts
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                counts["scanned"] += 1
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        counts["deleted"] += 1
                except OSError:
                    counts["errors"] += 1
                    _log.debug("gc_prefix: skip %s", path.name)
            return counts

        return await asyncio.to_thread(_sweep)


__all__ = ["LocalWorkspaceStorage"]
