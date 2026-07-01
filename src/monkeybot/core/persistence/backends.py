"""Storage backend protocols and factory for MonkeyBot persistence.

``backends.py`` is protocol-only — no implementation code is imported at module
level. Both ``SQLiteStorageBackend`` and ``PostgresStorageBackend`` are lazy-
imported inside :func:`create_storage_backend` so that neither SQLite nor
Postgres implementation code loads until the factory is actually called.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urlparse

if TYPE_CHECKING:
    from pathlib import Path

    from monkeybot.core.llm.provider import Message
    from monkeybot.core.llm.usage import Usage, UsageSummary
    from monkeybot.core.persistence.durable_runs import SubagentEnvelope, SubagentRunRow
    from monkeybot.core.persistence.scheduled_loops import (
        ScheduledLoopCreate,
        ScheduledLoopRow,
    )
    from monkeybot.core.persistence.thread_summary import ChatThreadSummary


@runtime_checkable
class HistoryStore(Protocol):
    """Protocol for conversation history persistence."""

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]: ...

    async def append(self, thread_id: str, message: Message) -> None: ...

    async def reset(self, thread_id: str, messages: list[Message]) -> None: ...

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]: ...


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

    async def record_pending(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None: ...

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
    ) -> None: ...

    async def claim(self, run_id: str, worker_id: str) -> bool: ...

    async def reset_stale_claims(self, stale_after_ms: int) -> int: ...

    async def record_completed(self, run_id: str, result_json: str) -> None: ...

    async def record_failed(self, run_id: str, error: str) -> None: ...

    async def pending_runs(self) -> list[SubagentRunRow]: ...

    async def get_run(self, run_id: str) -> SubagentRunRow | None: ...


@runtime_checkable
class ScheduledLoopStore(Protocol):
    """Protocol for prompt-first scheduled agent loops."""

    async def create(self, spec: ScheduledLoopCreate) -> ScheduledLoopRow: ...

    async def get(self, loop_id: str) -> ScheduledLoopRow | None: ...

    async def list_all(self) -> list[ScheduledLoopRow]: ...

    async def list_due(self, now_ms: int) -> list[ScheduledLoopRow]: ...

    async def claim_tick(self, loop_id: str, worker_id: str) -> ScheduledLoopRow | None: ...

    async def release_stale_claims(self, stale_after_ms: int) -> int: ...

    async def complete_tick(
        self,
        loop_id: str,
        *,
        worker_id: str,
        error: str | None = None,
    ) -> ScheduledLoopRow | None: ...

    async def defer_tick(self, loop_id: str, *, worker_id: str, reason: str) -> None: ...

    async def pause(self, loop_id: str) -> bool: ...

    async def resume(self, loop_id: str) -> bool: ...

    async def stop(self, loop_id: str, *, stop_reason: str = "manual") -> bool: ...


@runtime_checkable
class StorageBackend(Protocol):
    """Owns a storage connection lifecycle and vends all stores."""

    async def open(self, *, run_schema: bool = True) -> None: ...

    async def close(self) -> None: ...

    def history(self) -> HistoryStore: ...

    def usage(self) -> UsageStore: ...

    def runs(self) -> RunStore: ...

    def scheduled_loops(self) -> ScheduledLoopStore: ...


@dataclass(frozen=True)
class FirestoreConfig:
    """Parsed ``firestore://`` storage URL."""

    project: str
    database: str
    prefix: str = ""


def _normalize_postgres_db_url(db_url: str) -> str | None:
    """Return a ``postgresql://`` URL for Postgres backends, or ``None`` if not Postgres.

    Accepts both ``postgresql://`` (SQLAlchemy/asyncpg style) and ``postgres://``
    (common alias, e.g. Heroku-style URLs). asyncpg expects ``postgresql://``.
    """
    stripped = db_url.strip()
    if stripped.startswith("postgresql://"):
        return stripped
    if stripped.startswith("postgres://"):
        return "postgresql://" + stripped[len("postgres://") :]
    return None


def _parse_firestore_config(db_url: str) -> FirestoreConfig | None:
    """Return Firestore settings for ``firestore://`` URLs, or ``None`` if not Firestore."""
    stripped = db_url.strip()
    if not stripped.startswith("firestore://"):
        return None
    parsed = urlparse(stripped)
    project = unquote(parsed.hostname or "")
    if not project:
        raise ValueError(f"Firestore URL missing project id: {db_url!r}")
    database = unquote(parsed.path.lstrip("/")) or "(default)"
    prefix = parse_qs(parsed.query).get("prefix", [""])[0]
    return FirestoreConfig(project=project, database=database, prefix=prefix)


def create_storage_backend(db_url: str) -> StorageBackend:
    """Factory that returns the right backend for ``db_url``.

    Neither SQLite nor Postgres implementation code is imported until this
    function is called with a matching URL scheme.

    - ``sqlite://...`` → :class:`~monkeybot.core.persistence.sqlite_backend.SQLiteStorageBackend`
      (zero extra deps beyond core)
    - ``postgresql://...`` or ``postgres://...`` → :class:`~monkeybot.core.persistence.postgres.PostgresStorageBackend`
      (requires ``pip install 'monkeybot[postgres]'``)
    - ``firestore://PROJECT/DATABASE`` → :class:`~monkeybot.core.persistence.firestore.FirestoreStorageBackend`
      (requires ``pip install 'monkeybot[firestore]'``)
    """
    if db_url.startswith("sqlite://"):
        from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

        return SQLiteStorageBackend(db_url)
    pg_url = _normalize_postgres_db_url(db_url)
    if pg_url is not None:
        try:
            from monkeybot.core.persistence.postgres import PostgresStorageBackend
        except ImportError as exc:
            raise RuntimeError(
                "asyncpg is not installed. "
                "Run: pip install 'monkeybot[postgres]'"
            ) from exc
        return PostgresStorageBackend(pg_url)
    fs_config = _parse_firestore_config(db_url)
    if fs_config is not None:
        try:
            from monkeybot.core.persistence.firestore import FirestoreStorageBackend
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-firestore is not installed. "
                "Run: pip install 'monkeybot[firestore]'"
            ) from exc
        return FirestoreStorageBackend(fs_config)
    raise ValueError(f"Unsupported DB URL scheme: {db_url!r}")
