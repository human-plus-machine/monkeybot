"""Storage backend protocols and factory for MonkeyBot persistence.

``backends.py`` is protocol-only — no implementation code is imported at module
level. Both ``SQLiteStorageBackend`` and ``PostgresStorageBackend`` are lazy-
imported inside :func:`create_storage_backend` so that neither SQLite nor
Postgres implementation code loads until the factory is actually called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from monkeybot.core.llm.provider import Message
    from monkeybot.core.llm.usage import Usage, UsageSummary
    from monkeybot.core.persistence.durable_runs import SubagentEnvelope, SubagentRunRow


@runtime_checkable
class HistoryStore(Protocol):
    """Protocol for conversation history persistence."""

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]: ...

    async def append(self, thread_id: str, message: Message) -> None: ...

    async def reset(self, thread_id: str, messages: list[Message]) -> None: ...


@runtime_checkable
class UsageStore(Protocol):
    """Protocol for per-turn token/cost accounting."""

    async def record(
        self,
        thread_id: str,
        model: str,
        usage: Usage,
        run_id: str | None = None,
    ) -> None: ...

    async def summary(
        self,
        thread_id: str | None = None,
        since_ms: int | None = None,
    ) -> UsageSummary: ...


@runtime_checkable
class RunStore(Protocol):
    """Protocol for subagent run lifecycle tracking."""

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None: ...

    async def record_completed(self, run_id: str, result_json: str) -> None: ...

    async def record_failed(self, run_id: str, error: str) -> None: ...

    async def pending_runs(self) -> list[SubagentRunRow]: ...

    async def get_run(self, run_id: str) -> SubagentRunRow | None: ...


@runtime_checkable
class StorageBackend(Protocol):
    """Owns a storage connection lifecycle and vends all three stores."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    def history(self) -> HistoryStore: ...

    def usage(self) -> UsageStore: ...

    def runs(self) -> RunStore: ...


def create_storage_backend(db_url: str) -> StorageBackend:
    """Factory that returns the right backend for ``db_url``.

    Neither SQLite nor Postgres implementation code is imported until this
    function is called with a matching URL scheme.

    - ``sqlite://...`` → :class:`~monkeybot.core.persistence.sqlite_backend.SQLiteStorageBackend`
      (zero extra deps beyond core)
    - ``postgresql://...`` or ``postgres://...`` → :class:`~monkeybot.core.persistence.postgres.PostgresStorageBackend`
      (requires ``pip install 'monkeybot[postgres]'``)
    """
    if db_url.startswith("sqlite://"):
        from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

        return SQLiteStorageBackend(db_url)
    stripped = db_url.strip()
    if stripped.startswith("postgresql://"):
        pg_url = stripped
    elif stripped.startswith("postgres://"):
        pg_url = "postgresql://" + stripped[len("postgres://"):]
    else:
        raise ValueError(f"Unsupported DB URL scheme: {db_url!r}")
    try:
        from monkeybot.core.persistence.postgres import PostgresStorageBackend
    except ImportError as exc:
        raise RuntimeError(
            "asyncpg is not installed. "
            "Run: pip install 'monkeybot[postgres]'"
        ) from exc
    return PostgresStorageBackend(pg_url)
