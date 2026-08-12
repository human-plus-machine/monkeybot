"""Memory package: :class:`MemorySubsystem` is the public integration surface."""

from __future__ import annotations

from monkeybot.core.memory.compat import (
    INDEX_FILENAME,
    IntegrityResult,
    MemoryIntegrityChecker,
    MemoryPromotionError,
    async_load_index,
    async_promote_to_memory,
    async_search_memory_files,
)
from monkeybot.core.memory.config import memory_enabled_from_config
from monkeybot.core.memory.ingest import persist_message, visible_text
from monkeybot.core.memory.subsystem import MemorySubsystem

__all__ = [
    "INDEX_FILENAME",
    "IntegrityResult",
    "MemoryIntegrityChecker",
    "MemoryPromotionError",
    "MemorySubsystem",
    "async_load_index",
    "async_promote_to_memory",
    "async_search_memory_files",
    "memory_enabled_from_config",
    "persist_message",
    "visible_text",
]
