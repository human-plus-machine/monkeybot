"""Shared types and protocol for pluggable web search backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SearchResult:
    """Normalised result returned by every backend."""

    title: str
    url: str
    snippet: str
    score: float | None = None


@runtime_checkable
class WebSearchBackend(Protocol):
    """Protocol every web search backend must satisfy."""

    name: str

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]: ...
