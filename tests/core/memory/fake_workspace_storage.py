"""In-memory :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage` for unit tests."""

from __future__ import annotations


class FakeWorkspaceStorage:
    """POSIX-style keys → UTF-8 text; no directories (implicit from key prefixes)."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.gc_calls: list[tuple[str, float]] = []

    async def read_text(self, path: str) -> str:
        key = path.strip().replace("\\", "/").lstrip("/")
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    async def write_text(self, path: str, content: str) -> None:
        key = path.strip().replace("\\", "/").lstrip("/")
        self.files[key] = content

    async def append_text(self, path: str, content: str) -> None:
        key = path.strip().replace("\\", "/").lstrip("/")
        self.files[key] = self.files.get(key, "") + content

    async def exists(self, path: str) -> bool:
        key = path.strip().replace("\\", "/").lstrip("/")
        return key in self.files

    async def list_files(self, prefix: str = "") -> list[str]:
        pre = prefix.strip().replace("\\", "/")
        if pre and not pre.endswith("/"):
            pre = pre + "/"
        out: list[str] = []
        for k in sorted(self.files):
            if not pre:
                out.append(k)
            elif k.startswith(pre):
                out.append(k)
        return out

    async def delete(self, path: str) -> None:
        key = path.strip().replace("\\", "/").lstrip("/")
        self.files.pop(key, None)

    async def move(self, src: str, dest: str) -> None:
        sk = src.strip().replace("\\", "/").lstrip("/")
        dk = dest.strip().replace("\\", "/").lstrip("/")
        if sk not in self.files:
            raise FileNotFoundError(sk)
        self.files[dk] = self.files.pop(sk)

    async def gc_prefix(self, prefix: str, max_age_sec: float) -> dict[str, int]:
        self.gc_calls.append((prefix, max_age_sec))
        return {"scanned": 0, "deleted": 0, "errors": 0}


__all__ = ["FakeWorkspaceStorage"]
