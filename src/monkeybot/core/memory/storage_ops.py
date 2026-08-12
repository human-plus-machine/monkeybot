"""Deprecated import path. Use :mod:`monkeybot.core.memory.compat`."""

from __future__ import annotations

from monkeybot.core.memory.compat import (
    INDEX_FILENAME,
    MemoryPromotionError,
    async_load_index,
    async_promote_to_memory,
    async_search_memory_files,
)

__all__ = [
    "INDEX_FILENAME",
    "MemoryPromotionError",
    "async_load_index",
    "async_promote_to_memory",
    "async_search_memory_files",
]
