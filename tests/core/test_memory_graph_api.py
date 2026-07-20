"""Memory graph export for visualization."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.memory.graph import MemoryGraph


@pytest.mark.asyncio
async def test_memory_export_graph(tmp_path: Path) -> None:
    graph = MemoryGraph(tmp_path / ".graph.sqlite")
    await graph.open()
    try:
        await graph.upsert_note(
            "semantic/prefs.md",
            note_type="semantic",
            status="active",
            updated_at=1.0,
            links=[("episodic/meet.md", "related")],
        )
        await graph.upsert_note(
            "episodic/meet.md",
            note_type="episodic",
            status="active",
            updated_at=2.0,
            links=[],
        )
        payload = await graph.export_graph()
        paths = {n["path"] for n in payload["nodes"]}
        assert paths == {"semantic/prefs.md", "episodic/meet.md"}
        assert any(n["type"] == "semantic" for n in payload["nodes"])
        assert any(
            e["source"] == "semantic/prefs.md" and e["target"] == "episodic/meet.md"
            for e in payload["edges"]
        )
    finally:
        await graph.close()
