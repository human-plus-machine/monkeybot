"""Deprecated note-memory adapters kept so 2.2.x imports and routes do not break."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INDEX_FILENAME = "INDEX.md"


class MemoryPromotionError(RuntimeError):
    """Raised when a legacy promote-to-memory call cannot be honored."""


@dataclass
class IntegrityResult:
    ok: bool = True
    issues: list[str] = field(default_factory=list)


class MemoryIntegrityChecker:
    """No-op stand-in for the removed markdown integrity checker."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        warnings.warn(
            "MemoryIntegrityChecker is deprecated; MemPalace does not use INDEX.md",
            DeprecationWarning,
            stacklevel=2,
        )

    async def check(self) -> IntegrityResult:
        return IntegrityResult(ok=True)


async def async_load_index(storage: Any = None) -> list[str]:
    del storage
    warnings.warn(
        "async_load_index is deprecated; use MemorySubsystem.load_index",
        DeprecationWarning,
        stacklevel=2,
    )
    return []


async def async_search_memory_files(
    query: str,
    storage: Any = None,
    top_k: int = 5,
    **kwargs: Any,
) -> list[Any]:
    del query, storage, top_k, kwargs
    warnings.warn(
        "async_search_memory_files is deprecated; use `mempalace search` via run_command",
        DeprecationWarning,
        stacklevel=2,
    )
    return []


async def async_promote_to_memory(run_id: str, file: Path, storage: Any = None) -> None:
    del run_id, file, storage
    raise MemoryPromotionError(
        "promote_to_memory was removed; conversation turns ingest automatically via MemPalace"
    )
