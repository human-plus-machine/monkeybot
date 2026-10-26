"""T1-MEM-01..03: thread sharing, drain timeout, and object-store URIs."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from monkeybot.core.layout import resolve_memory_storage_uri
from monkeybot.core.memory.outbox import ensure_outbox_schema
from monkeybot.core.memory.palace import palace_path_from_uri
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from testkit.palace import InMemoryPalace

_THREAD_A = "thread-a"
_THREAD_B = "thread-b"
_FACT_A = "fact-from-thread-a"
_FACT_B = "fact-from-thread-b"


@pytest.fixture
async def db_url(tmp_path: Path) -> str:
    path = tmp_path / "monkeybot.db"
    url = f"sqlite:///{path}"
    conn = await open_connection(url)
    await apply_schema(conn)
    await ensure_outbox_schema(conn)
    await conn.close()
    return url


def _subsystem(
    tmp_path: Path,
    db_url: str,
    *,
    palace: InMemoryPalace | None = None,
) -> MemorySubsystem:
    palace = palace or InMemoryPalace(tmp_path / "agent-a" / "mempalace", agent_name="agent-a")
    return MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=db_url,
        agent_id="agent-a",
        agent_name="agent-a",
        palace=palace,
    )


async def _seed_both_threads(sub: MemorySubsystem) -> None:
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id=_THREAD_A,
        turn_id="1",
        message_id="m-a",
        role="user",
        content=_FACT_A,
    )
    await sub.enqueue(
        thread_id=_THREAD_B,
        turn_id="1",
        message_id="m-b",
        role="user",
        content=_FACT_B,
    )
    await sub.drain_writer(timeout_s=2)


async def _recall_b(sub: MemorySubsystem) -> list[str]:
    drawers = await sub.recall(wing="main", room="conversation", thread_id=_THREAD_B)
    return [drawer.content for drawer in drawers]


@pytest.mark.case("T1-MEM-01")
@pytest.mark.asyncio
async def test_default_share_thread_b_recalls_thread_a(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONKEYBOT_MEMORY_SHARE_THREADS", raising=False)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ID", raising=False)
    assert "MONKEYBOT_MEMORY_SHARE_THREADS" not in os.environ
    sub = _subsystem(tmp_path, db_url)
    await _seed_both_threads(sub)
    texts = await _recall_b(sub)
    assert _FACT_A in texts
    assert _FACT_B in texts
    await sub.close()


@pytest.mark.case("T1-MEM-01")
@pytest.mark.asyncio
async def test_share_threads_off_excludes_thread_a(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_MEMORY_SHARE_THREADS", "off")
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ID", raising=False)
    sub = _subsystem(tmp_path, db_url)
    await _seed_both_threads(sub)
    texts = await _recall_b(sub)
    assert _FACT_B in texts
    assert _FACT_A not in texts
    await sub.close()


class _HungEmbedder(InMemoryPalace):
    """Embedder that blocks until released, then writes."""

    def __init__(self, palace_path: Path, release: threading.Event) -> None:
        super().__init__(palace_path, agent_name="agent-a")
        self._release = release

    def upsert_drawer(self, drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        self._release.wait()
        super().upsert_drawer(drawer_id, content, metadata)


async def _outbox_statuses(db_url: str) -> list[str]:
    conn = await open_connection(db_url)
    try:
        cur = await conn.execute("SELECT status FROM memory_outbox ORDER BY message_id")
        rows = [str(row[0]) for row in await cur.fetchall()]
        await cur.close()
        return rows
    finally:
        await conn.close()


@pytest.mark.case("T1-MEM-02")
@pytest.mark.asyncio
async def test_hung_embedder_rows_stay_pending(db_url: str, tmp_path: Path) -> None:
    release = threading.Event()
    palace = _HungEmbedder(tmp_path / "hung" / "mempalace", release)
    sub = _subsystem(tmp_path, db_url, palace=palace)
    await sub.enqueue(
        thread_id=_THREAD_A,
        turn_id="1",
        message_id="m-hung",
        role="user",
        content="do not commit while the embedder is hung",
    )
    task = asyncio.create_task(sub.drain_writer(timeout_s=0.2))
    try:
        done, _pending = await asyncio.wait({task}, timeout=0.6)
        finished_in_time = bool(done)
        if finished_in_time:
            statuses = await _outbox_statuses(db_url)
            release.set()
            await task
        else:
            release.set()
            await task
            statuses = await _outbox_statuses(db_url)
        assert finished_in_time
        assert statuses == ["pending"]
    finally:
        release.set()
        if not task.done():
            await task
        await sub.close()


@pytest.mark.case("T1-MEM-03")
def test_object_store_uri_falls_back_and_palace_path_raises(tmp_path: Path) -> None:
    for raw in ("gcs://bucket/mem", "s3://bucket/mem"):
        uri = resolve_memory_storage_uri(raw, tmp_path)
        assert uri.startswith("local://")
        assert "gcs://" not in uri
        assert "s3://" not in uri
        local = Path(uri.removeprefix("local://"))
        assert local.is_absolute()
        assert tmp_path == local or tmp_path in local.parents
        with pytest.raises(ValueError):
            palace_path_from_uri(raw)
