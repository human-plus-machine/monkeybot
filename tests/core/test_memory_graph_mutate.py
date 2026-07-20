"""Memory graph + mutation tool tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.memory.note_format import format_memory_note, parse_memory_note
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.workspace.local import LocalWorkspaceStorage


class _FakeProvider:
    async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("provider should not be called")


@pytest.mark.asyncio
async def test_update_and_forget_memory(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "semantic").mkdir(parents=True)
    path = "semantic/old.md"
    storage = LocalWorkspaceStorage(mem)
    await storage.write_text(
        path,
        format_memory_note(
            note_type="semantic",
            status="active",
            body="The deploy tool is broken.",
        ),
    )
    await storage.write_text(
        "INDEX.md",
        "# Memory Index\n\n- [[semantic/old.md]] | tags: | summary: deploy broken\n",
    )
    sub = MemorySubsystem(
        storage=storage,
        provider=_FakeProvider(),  # type: ignore[arg-type]
        model="test",
        memory_uri=f"local://{mem}",
    )
    try:
        updated = await sub.update_memory(path, "The deploy tool works after the fix.")
        assert updated["ok"] is True
        new_path = updated["path"]
        old = await storage.read_text(path)
        old_meta, _ = parse_memory_note(old)
        assert old_meta is not None and old_meta.status == "superseded"
        new_text = await storage.read_text(new_path)
        new_meta, body = parse_memory_note(new_text)
        assert new_meta is not None and new_meta.status == "active"
        assert "works after the fix" in body

        search = await sub.search_files("deploy tool")
        paths = {h["path"] for h in search["hits"]}
        assert new_path in paths
        assert path not in paths  # superseded excluded by default

        forgotten = await sub.forget(new_path)
        assert forgotten["ok"] is True
        search2 = await sub.search_files("deploy tool")
        assert all(h["path"] != new_path for h in search2["hits"])
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_search_memory_folder_filter(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "episodic").mkdir(parents=True)
    (mem / "procedural").mkdir(parents=True)
    storage = LocalWorkspaceStorage(mem)
    await storage.write_text(
        "episodic/e.md",
        format_memory_note(note_type="episodic", status="active", body="event about widgets"),
    )
    await storage.write_text(
        "procedural/p.md",
        format_memory_note(
            note_type="procedural", status="active", body="how to build widgets"
        ),
    )
    sub = MemorySubsystem(
        storage=storage,
        provider=_FakeProvider(),  # type: ignore[arg-type]
        model="test",
        memory_uri=f"local://{mem}",
    )
    try:
        hits = await sub.search_files("widgets", folder="procedural")
        assert len(hits["hits"]) == 1
        assert hits["hits"][0]["path"].startswith("procedural/")
    finally:
        await sub.close()
