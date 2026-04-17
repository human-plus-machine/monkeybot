"""In-process reference backends used by the contract suite.

Every concrete extension surface ships a ``Mock*`` implementation that is
correct enough to satisfy the Story 1 contract invariants (see
``tests/harness/extensions/contracts/``). Subsequent stories reuse these as
the "known good" baseline when adding cloud backends to the same matrix.

The mocks deliberately avoid network I/O and are safe to instantiate in any
unit test environment.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import SecretStr

from .base import (
    Checkpointer,
    IdentitySource,
    JobStorage,
    MemoryStore,
    ModelProvider,
    SecretResolver,
)
from .errors import (
    CheckpointMissing,
    IdentityNotFound,
    ModelProviderError,
    SecretNotFound,
)
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

    from ..events import Principal
    from ..specs import AgentSpec


class MockCheckpointer(Checkpointer):
    """In-memory :class:`Checkpointer` with monotonic per-session ids."""

    def __init__(self) -> None:
        self._payloads: dict[tuple[str, str], Mapping[str, Any]] = {}
        self._refs: dict[str, list[CheckpointRef]] = {}
        self._counters: dict[str, itertools.count[int]] = {}
        self._lock = asyncio.Lock()

    def _next_id(self, session_id: str) -> str:
        counter = self._counters.setdefault(session_id, itertools.count(1))
        seq = next(counter)
        return f"{seq:012d}-{uuid.uuid4().hex[:8]}"

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        async with self._lock:
            checkpoint_id = self._next_id(session_id)
            payload = dict(state)
            ref = CheckpointRef(
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                reason=reason,
                created_at=datetime.now(UTC),
                bytes=len(repr(payload).encode()),
                uri=f"mock://{session_id}/{checkpoint_id}",
            )
            self._payloads[(session_id, checkpoint_id)] = payload
            self._refs.setdefault(session_id, []).append(ref)
            return ref

    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        refs = self._refs.get(session_id, [])
        if checkpoint_id is None:
            if not refs:
                return None
            latest = refs[-1]
            return self._payloads.get((session_id, latest.checkpoint_id))
        payload = self._payloads.get((session_id, checkpoint_id))
        if payload is None:
            raise CheckpointMissing(session_id, checkpoint_id)
        return payload

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        refs = list(reversed(self._refs.get(session_id, [])))
        return refs[:limit]

    async def delete_session(self, session_id: str) -> None:
        refs = self._refs.pop(session_id, [])
        for ref in refs:
            self._payloads.pop((session_id, ref.checkpoint_id), None)
        self._counters.pop(session_id, None)


class MockMemoryStore(MemoryStore):
    """In-memory :class:`MemoryStore` supporting filter/TTL semantics."""

    def __init__(self) -> None:
        self._items: dict[tuple[tuple[str, ...], str], tuple[Item, datetime | None]] = {}

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        now = datetime.now(UTC)
        existing = self._items.get((namespace, key))
        created_at = existing[0].created_at if existing else now
        expires_at = now + ttl if ttl is not None else None
        item = Item(
            namespace=namespace,
            key=key,
            value=dict(value),
            created_at=created_at,
            updated_at=now,
        )
        self._items[(namespace, key)] = (item, expires_at)

    def _live_item(self, key: tuple[tuple[str, ...], str]) -> Item | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        item, expires_at = entry
        if expires_at is not None and expires_at < datetime.now(UTC):
            del self._items[key]
            return None
        return item

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        return self._live_item((namespace, key))

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        results: list[Item] = []
        for (ns, _key), (item, _exp) in list(self._items.items()):
            if ns != namespace:
                continue
            live = self._live_item((ns, item.key))
            if live is None:
                continue
            if filter and not all(live.value.get(fk) == fv for fk, fv in filter.items()):
                continue
            if query is not None and query not in repr(live.value):
                continue
            results.append(live)
            if len(results) >= limit:
                break
        return results

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        self._items.pop((namespace, key), None)

    async def list_namespaces(self, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        seen: set[tuple[str, ...]] = set()
        for (ns, _key), _entry in self._items.items():
            if len(ns) < len(prefix):
                continue
            if ns[: len(prefix)] != prefix:
                continue
            seen.add(ns)
        return sorted(seen)

    def capabilities(self) -> MemoryStoreCapabilities:
        return MemoryStoreCapabilities(
            vector_search=False,
            keyword_search=True,
            namespace_listing=True,
            ttl=True,
            transactional=False,
        )


class MockJobStorage(JobStorage):
    """In-memory :class:`JobStorage` with an asyncio lock guarding lease state."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def load_jobs(self) -> list[Mapping[str, Any]]:
        async with self._lock:
            return [dict(job) for job in self._jobs.values()]

    async def save_jobs(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        async with self._lock:
            self._jobs.clear()
            for job in jobs:
                job_id = str(job["job_id"])
                self._jobs[job_id] = dict(job)

    async def claim_job(self, job_id: str, lease_duration_seconds: int = 300) -> bool:
        async with self._lock:
            now = datetime.now(UTC)
            current = self._leases.get(job_id)
            if current is not None and current > now:
                return False
            if job_id not in self._jobs:
                self._jobs[job_id] = {"job_id": job_id}
            self._leases[job_id] = now + timedelta(seconds=lease_duration_seconds)
            return True

    async def release_job(self, job_id: str) -> None:
        async with self._lock:
            self._leases.pop(job_id, None)

    async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None


class MockIdentitySource(IdentitySource):
    """In-memory :class:`IdentitySource` with an allowlist of principals."""

    def __init__(self, allowlist: Iterable[str] = ("alice", "bob")) -> None:
        self._allowlist = set(allowlist)
        self._memory: dict[str, str] = {}
        self._heartbeat: dict[str, str] = {}

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        if principal.id not in self._allowlist:
            raise IdentityNotFound(principal.id)
        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul="mock soul",
            rules="mock rules",
            identity="mock identity",
            user="mock user",
            index="mock index",
            memory=self._memory.get(principal.id, ""),
            heartbeat=self._heartbeat.get(principal.id, ""),
            loaded_at=datetime.now(UTC),
            ttl_seconds=300,
            source_backend="mock",
        )

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        if principal.id not in self._allowlist:
            raise IdentityNotFound(principal.id)
        target_map = self._memory if patch.target == "MEMORY.md" else self._heartbeat
        existing = target_map.get(principal.id, "")
        if patch.operation == "append":
            target_map[principal.id] = existing + (patch.content or "")
        elif patch.operation == "replace":
            target_map[principal.id] = patch.content or ""
        else:
            target_map.pop(principal.id, None)


class MockSecretResolver(SecretResolver):
    """In-memory :class:`SecretResolver` backed by a preset dict."""

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = dict(secrets or {"KNOWN_HANDLE": "the-secret"})

    async def resolve(self, handle: str) -> SecretStr:
        value = self._secrets.get(handle)
        if value is None:
            raise SecretNotFound(handle)
        return SecretStr(value)


class MockModelProvider(ModelProvider):
    """Returns a LangChain ``FakeListChatModel`` for deterministic tests."""

    def __init__(self, responses: Sequence[str] | None = None) -> None:
        self._responses = list(responses or ["mock response"])

    def build(self, spec: AgentSpec) -> BaseChatModel:
        try:
            from langchain_core.language_models import FakeListChatModel
        except ImportError as exc:  # pragma: no cover - langchain is a runtime dep
            raise ModelProviderError("langchain_core is required for MockModelProvider") from exc
        return FakeListChatModel(responses=list(self._responses))

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()


__all__ = [
    "MockCheckpointer",
    "MockIdentitySource",
    "MockJobStorage",
    "MockMemoryStore",
    "MockModelProvider",
    "MockSecretResolver",
]
