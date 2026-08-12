"""Deprecated note-graph module. MemPalace drawers are exported via MemorySubsystem."""

from __future__ import annotations

from typing import Any


class MemoryGraphStore:
    """No-op stand-in for the removed markdown memory graph."""

    async def export_graph(self) -> dict[str, Any]:
        return {"nodes": [], "edges": []}

    async def neighbors(self, path: str) -> list[str]:
        del path
        return []

    async def close(self) -> None:
        return


class MemoryGraph(MemoryGraphStore):
    """Alias kept for library imports that constructed MemoryGraph directly."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


__all__ = ["MemoryGraph", "MemoryGraphStore"]
