"""Single injection point for memory (storage + hook + organizer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Provider
from monkeybot.core.memory.hook import MemoryHook
from monkeybot.core.memory.organizer import MemoryOrganizer
from monkeybot.core.memory.storage_ops import (
    async_load_index,
    async_promote_to_memory,
    async_search_memory_files,
)
from monkeybot.core.workspace.protocol import WorkspaceStorage


class MemorySubsystem:
    """Owns :class:`WorkspaceStorage`, :class:`MemoryHook`, and :class:`MemoryOrganizer`.

    External callers inject this object into :func:`~monkeybot.core.context.build_context`
    and :class:`~monkeybot.core.tools.core_tool_executor.CoreToolExecutor` instead of
    passing paths or storage separately.
    """

    def __init__(
        self,
        storage: WorkspaceStorage,
        provider: Provider,
        model: str,
        *,
        memory_uri: str,
        max_retrieval_hits: int = 3,
    ) -> None:
        self._storage = storage
        self._memory_uri = memory_uri.strip()
        self._organizer = MemoryOrganizer(
            provider=provider,
            model=model,
            storage=storage,
        )
        self._hook = MemoryHook(
            storage=storage,
            organizer_runner=self._organizer.run,
            max_retrieval_hits=max_retrieval_hits,
        )

    @property
    def uri(self) -> str:
        """URI string used to reconstruct storage in subprocess workers."""
        return self._memory_uri

    @property
    def storage(self) -> WorkspaceStorage:
        return self._storage

    def register_hooks(self, manager: HookManager) -> None:
        self._hook.register(manager)

    async def load_index(self) -> list[str]:
        return await async_load_index(self._storage)

    async def search_files(
        self,
        query: str,
        *,
        max_hits: int = 40,
        skip_raw: bool = True,
    ) -> dict[str, Any]:
        skip: tuple[str, ...] = ("raw",) if skip_raw else ()
        return await async_search_memory_files(
            self._storage,
            query,
            max_hits=max_hits,
            skip_relative_prefixes=skip,
        )

    async def promote(self, run_id: str, file: Path) -> None:
        await async_promote_to_memory(run_id, file, self._storage)

    async def gc_processed(self, *, max_age_sec: float = 7 * 24 * 60 * 60) -> dict[str, int]:
        return await self._storage.gc_prefix("raw/processed/", max_age_sec)

    async def flush(self) -> None:
        """Await pending memory organizer task. See :meth:`MemoryHook.flush`."""
        await self._hook.flush()


__all__ = ["MemorySubsystem"]
