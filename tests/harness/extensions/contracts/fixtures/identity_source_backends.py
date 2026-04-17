"""Story 5 contract fixtures for :class:`IdentitySource` backends.

Only backends that do not require external infrastructure are exposed
through the default parametrisation. Cloud backends (S3 / GCS / Postgres /
Mongo) rely on their own integration tests where the infra is available.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.harness.events import Principal
from src.core.harness.extensions import (
    CallableIdentitySource,
    IdentityNotFound,
    LocalFSIdentitySource,
)
from src.core.harness.extensions.values import LoadedIdentity


def _seed_local_fs_root() -> Path:
    """Create a temp directory pre-populated with the ``alice`` + ``bob`` principals."""
    root = Path(tempfile.mkdtemp(prefix="id-contract-"))
    for principal_id in ("alice", "bob"):
        principal_dir = root / principal_id
        principal_dir.mkdir(parents=True, exist_ok=True)
        for name in ("SOUL", "IDENTITY", "USER", "INDEX", "RULES", "MEMORY", "HEARTBEAT"):
            (principal_dir / f"{name}.md").write_text(f"{name.lower()}-body")
    return root


def _local_fs_factory() -> LocalFSIdentitySource:
    return LocalFSIdentitySource(dir=str(_seed_local_fs_root()))


def _callable_factory() -> CallableIdentitySource:
    async def fn(principal: Principal, session_id: str | None) -> LoadedIdentity:
        if principal.id == "nobody":
            raise IdentityNotFound(principal.id)
        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul="soul-body",
            rules="rules-body",
            identity="identity-body",
            user="user-body",
            index="index-body",
            memory="memory-body",
            heartbeat="heartbeat-body",
            loaded_at=datetime.now(UTC),
            ttl_seconds=60,
            source_backend="callable",
        )

    return CallableIdentitySource(fn)


IDENTITY_SOURCE_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("local_fs", _local_fs_factory),
    ("callable", _callable_factory),
]

__all__ = ["IDENTITY_SOURCE_FACTORIES"]
