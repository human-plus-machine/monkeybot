"""Memory package: :class:`MemorySubsystem` is the public integration surface."""

from __future__ import annotations

from monkeybot.core.memory.integrity import IntegrityResult, MemoryIntegrityChecker
from monkeybot.core.memory.storage_ops import (
    INDEX_FILENAME,
    MemoryPromotionError,
    async_load_index,
    async_promote_to_memory,
    async_search_memory_files,
)
from monkeybot.core.memory.subsystem import MemorySubsystem

__all__ = [
    "MemorySubsystem",
    "MemoryIntegrityChecker",
    "IntegrityResult",
    "INDEX_FILENAME",
    "MemoryPromotionError",
    "async_load_index",
    "async_search_memory_files",
    "async_promote_to_memory",
]
