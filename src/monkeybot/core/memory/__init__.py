"""Memory package: :class:`MemorySubsystem` is the public integration surface."""

from __future__ import annotations

from monkeybot.core.memory.ingest import persist_message, visible_text
from monkeybot.core.memory.subsystem import MemorySubsystem

__all__ = [
    "MemorySubsystem",
    "persist_message",
    "visible_text",
]
