"""Local filesystem-backed :class:`IdentitySource` (Story 5).

Wraps the legacy :class:`IdentityLoader` so each principal gets a private
directory of SOUL/IDENTITY/USER/INDEX/RULES/MEMORY/HEARTBEAT files.

Layout when ``per_principal_subdir=True`` (the default):

    <dir>/<principal_id>/SOUL.md
    <dir>/<principal_id>/RULES.md
    ...

Set ``per_principal_subdir=False`` to share a single identity across
principals (test fixtures, bootstrapping).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...identity import IdentityLoader
from ...specs import IdentitySpec
from ..base import IdentitySource
from ..errors import IdentityNotFound
from ..values import LoadedIdentity, MemoryPatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...events import Principal


class LocalFSIdentitySource(IdentitySource):
    """Read (and optionally write) identity files from the local filesystem.

    Args:
        dir: Root directory containing principal subdirectories (or the
            flat identity files when ``per_principal_subdir=False``).
        per_principal_subdir: When ``True`` (default), each principal id
            becomes a subdirectory under ``dir``.
        soul_file / rules_file / identity_file / user_file / index_file /
        memory_file / heartbeat_file: Filenames inside each principal dir.
        enforce_rules: Propagated to the underlying :class:`IdentityLoader`
            — when ``True`` a missing ``RULES.md`` raises.
        cache_ttl_seconds: Advertised TTL placed on the returned
            :class:`LoadedIdentity`. :class:`IdentityResolutionMW` treats
            this as the upper bound for its LRU entry.
    """

    def __init__(
        self,
        *,
        dir: str = "./data/memory",
        per_principal_subdir: bool = True,
        soul_file: str = "SOUL.md",
        rules_file: str = "RULES.md",
        identity_file: str = "IDENTITY.md",
        user_file: str = "USER.md",
        index_file: str = "INDEX.md",
        memory_file: str = "MEMORY.md",
        heartbeat_file: str = "HEARTBEAT.md",
        enforce_rules: bool = False,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self.dir = dir
        self.per_principal_subdir = per_principal_subdir
        self.soul_file = soul_file
        self.rules_file = rules_file
        self.identity_file = identity_file
        self.user_file = user_file
        self.index_file = index_file
        self.memory_file = memory_file
        self.heartbeat_file = heartbeat_file
        self.enforce_rules = enforce_rules
        self.cache_ttl_seconds = cache_ttl_seconds

    def _principal_dir(self, principal_id: str) -> Path:
        base = Path(self.dir)
        return base / principal_id if self.per_principal_subdir else base

    def _spec_for(self, principal_dir: Path) -> IdentitySpec:
        return IdentitySpec(
            dir=str(principal_dir),
            soul_file=self.soul_file,
            rules_file=self.rules_file,
            identity_file=self.identity_file,
            user_file=self.user_file,
            index_file=self.index_file,
            memory_file=self.memory_file,
            heartbeat_file=self.heartbeat_file,
            enforce_rules=self.enforce_rules,
        )

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Read the principal's identity directory and return a :class:`LoadedIdentity`."""
        principal_dir = self._principal_dir(principal.id)
        if not principal_dir.exists():
            raise IdentityNotFound(principal.id)

        def _load_sync() -> LoadedIdentity:
            legacy = IdentityLoader(self._spec_for(principal_dir)).load()
            return LoadedIdentity(
                principal_id=principal.id,
                session_id=session_id,
                soul=legacy.soul,
                rules=legacy.rules,
                identity=legacy.identity,
                user=legacy.user,
                index=legacy.index,
                memory=legacy.memory,
                heartbeat=legacy.heartbeat,
                loaded_at=datetime.now(UTC),
                ttl_seconds=self.cache_ttl_seconds,
                source_backend="local_fs",
                extras={},
            )

        return await asyncio.to_thread(_load_sync)

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        """Apply ``patch`` to the principal's MEMORY.md / HEARTBEAT.md file."""
        principal_dir = self._principal_dir(principal.id)
        filename = self.memory_file if patch.target == "MEMORY.md" else self.heartbeat_file

        def _apply() -> None:
            principal_dir.mkdir(parents=True, exist_ok=True)
            target = principal_dir / filename
            existing = target.read_text() if target.exists() else ""
            if patch.operation == "append":
                target.write_text(existing + (patch.content or ""))
            elif patch.operation == "replace":
                target.write_text(patch.content or "")
            else:
                if target.exists():
                    target.unlink()

        await asyncio.to_thread(_apply)


__all__ = ["LocalFSIdentitySource"]
