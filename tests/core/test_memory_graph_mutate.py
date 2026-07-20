"""Memory graph + mutation tool tests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.memory.graph import MemoryGraph
from monkeybot.core.memory.note_format import format_memory_note, parse_memory_note
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.workspace.local import LocalWorkspaceStorage


class _FakeProvider:
    async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("provider should not be called")


def _subsystem(mem: Path, *, graph: MemoryGraph | None = None) -> MemorySubsystem:
    return MemorySubsystem(
        storage=LocalWorkspaceStorage(mem),
        provider=_FakeProvider(),  # type: ignore[arg-type]
        model="test",
        memory_uri=f"local://{mem}",
        graph=graph,
    )


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
    sub = _subsystem(mem)
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

        index = await storage.read_text("INDEX.md")
        assert "[[semantic/old.md]]" not in index
        assert f"[[{new_path}]]" in index

        search = await sub.search_files("deploy tool")
        paths = {h["path"] for h in search["hits"]}
        assert new_path in paths
        assert path not in paths  # superseded excluded by default

        forgotten = await sub.forget(new_path)
        assert forgotten["ok"] is True
        index2 = await storage.read_text("INDEX.md")
        assert f"[[{new_path}]]" not in index2
        search2 = await sub.search_files("deploy tool")
        assert all(h["path"] != new_path for h in search2["hits"])
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_update_memory_rejects_non_typed_paths(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    storage = LocalWorkspaceStorage(mem)
    await storage.write_text("chat_log.md", "secret log\n")
    await storage.write_text("raw/tool.md", "raw dump\n")
    sub = _subsystem(mem)
    try:
        for bad in ("chat_log.md", "raw/tool.md", "INDEX.md"):
            result = await sub.update_memory(bad, "overwrite")
            assert result["ok"] is False
            assert "episodic|semantic|procedural|working" in result["error"]
        assert await storage.read_text("chat_log.md") == "secret log\n"
        assert await storage.read_text("raw/tool.md") == "raw dump\n"
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_update_memory_survives_graph_failure(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "semantic").mkdir(parents=True)
    path = "semantic/old.md"
    storage = LocalWorkspaceStorage(mem)
    await storage.write_text(
        path,
        format_memory_note(note_type="semantic", status="active", body="old fact"),
    )
    await storage.write_text(
        "INDEX.md",
        "# Memory Index\n\n- [[semantic/old.md]] | tags: | summary: old\n",
    )

    class _BrokenGraph:
        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def upsert_note(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("sqlite locked")

        async def set_status(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("sqlite locked")

        async def delete_note(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("sqlite locked")

        async def get_updated_at(self, path: str) -> float | None:
            del path
            return None

        async def get_status(self, path: str) -> str | None:
            del path
            return None

        async def neighbors(self, path: str) -> list[str]:
            del path
            return []

        async def list_paths(self, *, status: str | None = "active") -> list[str]:
            del status
            return []

        async def export_graph(self) -> dict[str, Any]:
            return {"nodes": [], "edges": []}

    sub = _subsystem(mem, graph=_BrokenGraph())  # type: ignore[arg-type]
    try:
        updated = await sub.update_memory(path, "new fact survives graph failure")
        assert updated["ok"] is True
        new_path = updated["path"]
        assert "new fact survives" in await storage.read_text(new_path)
        old_meta, _ = parse_memory_note(await storage.read_text(path))
        assert old_meta is not None and old_meta.status == "superseded"
        index = await storage.read_text("INDEX.md")
        assert "[[semantic/old.md]]" not in index
        assert f"[[{new_path}]]" in index
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_forget_drops_index_even_if_graph_fails(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "semantic").mkdir(parents=True)
    path = "semantic/gone.md"
    storage = LocalWorkspaceStorage(mem)
    await storage.write_text(
        path,
        format_memory_note(note_type="semantic", status="active", body="forget me"),
    )
    await storage.write_text(
        "INDEX.md",
        "# Memory Index\n\n- [[semantic/gone.md]] | tags: | summary: forget me\n",
    )

    class _BrokenGraph:
        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def upsert_note(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("boom")

        async def set_status(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("boom")

        async def delete_note(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("boom")

        async def get_updated_at(self, path: str) -> float | None:
            del path
            return None

        async def get_status(self, path: str) -> str | None:
            del path
            return None

        async def neighbors(self, path: str) -> list[str]:
            del path
            return []

        async def list_paths(self, *, status: str | None = "active") -> list[str]:
            del status
            return []

        async def export_graph(self) -> dict[str, Any]:
            return {"nodes": [], "edges": []}

    sub = _subsystem(mem, graph=_BrokenGraph())  # type: ignore[arg-type]
    try:
        result = await sub.forget(path)
        assert result["ok"] is True
        index = await storage.read_text("INDEX.md")
        assert "[[semantic/gone.md]]" not in index
        meta, _ = parse_memory_note(await storage.read_text(path))
        assert meta is not None and meta.status == "forgotten"
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_gc_working_uses_storage_mtime_without_graph(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "working").mkdir(parents=True)
    storage = LocalWorkspaceStorage(mem)
    path = "working/stale.md"
    await storage.write_text(
        path,
        format_memory_note(note_type="working", status="active", body="scratch"),
    )
    # Age the file beyond TTL
    stale = time.time() - 10 * 24 * 60 * 60
    (mem / "working" / "stale.md").touch()
    import os

    os.utime(mem / "working" / "stale.md", (stale, stale))

    # Inject a remote-style subsystem with no graph by using a non-local URI
    # while keeping local storage for the test.
    sub = MemorySubsystem(
        storage=storage,
        provider=_FakeProvider(),  # type: ignore[arg-type]
        model="test",
        memory_uri="gcs://bucket/memory",
        graph=None,
    )
    try:
        assert await sub.ensure_graph() is None
        result = await sub.gc_working(ttl_days=7)
        assert result["deleted"] == 1
        assert not await storage.exists(path)
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
    sub = _subsystem(mem)
    try:
        hits = await sub.search_files("widgets", folder="procedural")
        assert len(hits["hits"]) == 1
        assert hits["hits"][0]["path"].startswith("procedural/")
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_graph_opens_with_wal(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite"
    graph = MemoryGraph(db)
    await graph.open()
    try:
        # busy_timeout / WAL applied — upsert should succeed
        await graph.upsert_note(
            "semantic/a.md", note_type="semantic", status="active", updated_at=1.0
        )
        assert await graph.get_updated_at("semantic/a.md") == 1.0
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_mutation_and_organizer_share_lock(tmp_path: Path) -> None:
    """forget mid-turn and organizer INDEX write cannot interleave."""
    import asyncio

    mem = tmp_path / "memory"
    (mem / "semantic").mkdir(parents=True)
    storage = LocalWorkspaceStorage(mem)
    path = "semantic/shared.md"
    await storage.write_text(
        path,
        format_memory_note(note_type="semantic", status="active", body="shared note"),
    )
    await storage.write_text(
        "INDEX.md",
        "# Memory Index\n\n- [[semantic/shared.md]] | tags: | summary: shared\n",
    )

    sub = _subsystem(mem)
    try:
        order: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_organizer() -> None:
            async with sub.lock:
                order.append("organizer_enter")
                started.set()
                await release.wait()
                # Simulate INDEX rewrite while holding the shared lock.
                await storage.write_text(
                    "INDEX.md",
                    "# Memory Index\n\n- [[semantic/shared.md]] | tags: | summary: organizer\n",
                )
                order.append("organizer_exit")

        async def forget_while_organizer_holds() -> dict[str, Any]:
            await started.wait()
            order.append("forget_wait")
            result = await sub.forget(path)
            order.append("forget_done")
            return result

        org_task = asyncio.create_task(slow_organizer())
        forget_task = asyncio.create_task(forget_while_organizer_holds())
        await started.wait()
        await asyncio.sleep(0.05)
        # forget must still be waiting on the lock
        assert "forget_done" not in order
        release.set()
        forgotten = await forget_task
        await org_task
        assert forgotten["ok"] is True
        assert order == [
            "organizer_enter",
            "forget_wait",
            "organizer_exit",
            "forget_done",
        ]
        index = await storage.read_text("INDEX.md")
        assert "[[semantic/shared.md]]" not in index
    finally:
        await sub.close()
