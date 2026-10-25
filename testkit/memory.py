"""Shared MemorySubsystem factory for tests (InMemoryPalace, no embedder)."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.memory.subsystem import MemorySubsystem
from testkit.palace import InMemoryPalace


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


def mem_sub(root: Path) -> MemorySubsystem:
    """Palace files sit outside ``root`` so workspace grep/glob cannot see them."""
    resolved = Path(root).resolve()
    return make_memory_subsystem(
        resolved.parent.parent / f"palace-{resolved.parent.name}-{resolved.name}"
    )


def trace_mem_sub(root: Path) -> MemorySubsystem:
    """Subagent-trace copy: the palace lives inside ``root``.

    Not the same as ``mem_sub``. Kept separate for D7.
    """
    return make_memory_subsystem(root)
