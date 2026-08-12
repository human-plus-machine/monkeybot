"""Shared MemorySubsystem factory for tests (InMemoryPalace, no embedder)."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.memory.palace import InMemoryPalace
from monkeybot.core.memory.subsystem import MemorySubsystem


def make_memory_subsystem(
    root: Path,
    *,
    agent_id: str = "test-agent",
    writer_enabled: bool = False,
) -> MemorySubsystem:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    palace_path = root / "mempalace"
    palace = InMemoryPalace(palace_path, agent_name=agent_id)
    return MemorySubsystem(
        memory_uri=f"local://{palace_path}",
        db_url=f"sqlite:///{root / 'monkeybot.db'}",
        agent_id=agent_id,
        agent_name=agent_id,
        palace=palace,
        writer_enabled=writer_enabled,
    )
