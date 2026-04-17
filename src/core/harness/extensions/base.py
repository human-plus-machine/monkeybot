"""Abstract base classes for every harness extension surface.

See 1b-contracts.md §§3.1-3.6 for the authoritative signatures. Each ABC owns
its own ``Registry`` instance; concrete backends register against these at
import time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import SecretStr

from .registry import Registry
from .values import (
    CheckpointRef,
    Item,
    LoadedIdentity,
    MemoryPatch,
    MemoryStoreCapabilities,
    ModelCapabilities,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.store.base import BaseStore

    from ..events import Principal
    from ..specs import AgentSpec


class Checkpointer(ABC):
    """Durable per-session state store.

    See 1b-contracts.md §3.1. Concrete backends must implement all four
    abstract methods; ``gc`` is optional (default raises ``NotImplementedError``).
    """

    registry: ClassVar[Registry[Checkpointer]] = Registry("checkpointer")

    @abstractmethod
    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        """Persist ``state`` for ``session_id`` and return a :class:`CheckpointRef`."""

    @abstractmethod
    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return a previously-written state, or ``None`` if none exists."""

    @abstractmethod
    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        """Return checkpoint refs for ``session_id`` ordered newest-first."""

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """Delete every checkpoint row belonging to ``session_id``."""

    async def gc(self, older_than: timedelta) -> int:
        """Optional: delete checkpoints older than ``older_than``.

        Returns the number of removed rows. Default backends raise
        ``NotImplementedError`` to indicate GC is opt-in.
        """
        raise NotImplementedError


class MemoryStore(ABC):
    """Durable namespaced key/value store with optional vector search.

    See 1b-contracts.md §3.2. Consumers use ``as_langgraph_store()`` when a
    compiled LangGraph graph needs a ``BaseStore`` adapter.
    """

    registry: ClassVar[Registry[MemoryStore]] = Registry("memory_store")

    @abstractmethod
    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Write ``value`` under ``(namespace, key)`` with optional TTL."""

    @abstractmethod
    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the :class:`Item` at ``(namespace, key)`` or ``None``."""

    @abstractmethod
    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return items matching ``query``/``filter`` under ``namespace``."""

    @abstractmethod
    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete the item at ``(namespace, key)`` if present."""

    @abstractmethod
    async def list_namespaces(self, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        """Return distinct namespaces under ``prefix``."""

    def capabilities(self) -> MemoryStoreCapabilities:
        """Return the backend's declared capabilities (default: conservative)."""
        return MemoryStoreCapabilities()

    def as_langgraph_store(self) -> BaseStore:
        """Return a LangGraph ``BaseStore`` adapter for this store.

        Default raises ``NotImplementedError``; story 3 ships the concrete
        adapter used by LangGraph's ``@entrypoint.langgraph(store=...)`` path.
        """
        raise NotImplementedError


class JobStorage(ABC):
    """Atomic job-lease storage for the scheduler.

    See 1b-contracts.md §3.3. ``claim_job`` must be atomic at the row/document
    level for the lease to be correct under concurrency.
    """

    registry: ClassVar[Registry[JobStorage]] = Registry("job_storage")

    @abstractmethod
    async def load_jobs(self) -> list[Mapping[str, Any]]:
        """Return the full job list (for admin / restart flows)."""

    @abstractmethod
    async def save_jobs(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        """Replace the job list with ``jobs``."""

    @abstractmethod
    async def claim_job(self, job_id: str, lease_duration_seconds: int = 300) -> bool:
        """Attempt to claim ``job_id`` for ``lease_duration_seconds``.

        Returns ``True`` on successful lease, ``False`` if already held.
        """

    @abstractmethod
    async def release_job(self, job_id: str) -> None:
        """Release a previously-claimed job so another worker can claim it."""

    async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        """Optional: return a single job document. Default raises ``NotImplementedError``."""
        raise NotImplementedError


class IdentitySource(ABC):
    """Produces :class:`LoadedIdentity` values for a :class:`Principal`.

    See 1b-contracts.md §3.4. ``load`` must return the full bundle of identity
    artefacts; ``write_memory`` is optional and only implemented by sources
    that back ``MEMORY.md``/``HEARTBEAT.md`` with a mutable store.
    """

    registry: ClassVar[Registry[IdentitySource]] = Registry("identity_source")

    @abstractmethod
    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Resolve a :class:`LoadedIdentity` for ``principal``."""

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        """Optional: apply ``patch`` to the principal's mutable identity files.

        Default raises ``NotImplementedError``.
        """
        raise NotImplementedError


class SecretResolver(ABC):
    """Resolve opaque handles (e.g. ``"DATABASE_PASSWORD"``) to ``SecretStr``.

    See 1b-contracts.md §3.5.
    """

    registry: ClassVar[Registry[SecretResolver]] = Registry("secret_resolver")

    @abstractmethod
    async def resolve(self, handle: str) -> SecretStr:
        """Return the ``SecretStr`` value bound to ``handle``."""


class ModelProvider(ABC):
    """Factory for ``BaseChatModel`` instances.

    See 1b-contracts.md §3.6. ``build(spec)`` returns a fully-configured
    ``BaseChatModel`` ready to be bound with tools and invoked.
    """

    registry: ClassVar[Registry[ModelProvider]] = Registry("model_provider")

    @abstractmethod
    def build(self, spec: AgentSpec) -> BaseChatModel:
        """Build a ``BaseChatModel`` from ``spec``."""

    def capabilities(self) -> ModelCapabilities:
        """Return the provider-declared model capabilities."""
        return ModelCapabilities()


__all__ = [
    "Checkpointer",
    "IdentitySource",
    "JobStorage",
    "MemoryStore",
    "ModelProvider",
    "SecretResolver",
]
