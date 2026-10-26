"""T2-MEM-03: an ambiguous commit retries once and does not duplicate rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.memory.ingest import persist_message
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.errors import AmbiguousCommitError
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.types.content_blocks import Text
from testkit.palace import InMemoryPalace


class _AckLostHistory:
    """Commits the real row, then drops the acknowledgement on the first call."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def append_with_outbox(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        await self._inner.append_with_outbox(*args, **kwargs)  # type: ignore[attr-defined]
        if self.calls == 1:
            raise AmbiguousCommitError("ack lost")


@pytest.mark.case("T2-MEM-03")
async def test_ambiguous_commit_retries_once_and_keeps_one_row(tmp_path: Path) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'commit.db'}")
    await backend.open()
    palace = InMemoryPalace(tmp_path / "palace", agent_name="loop-scenario")
    memory = MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=f"sqlite:///{tmp_path / 'commit.db'}",
        agent_id="loop-scenario",
        agent_name="loop-scenario",
        palace=palace,
        storage=backend,
        writer_enabled=False,
    )
    history = _AckLostHistory(backend.history())
    try:
        await persist_message(
            history,  # type: ignore[arg-type]
            Message(role="user", content=[Text(text="persist me")]),
            thread_id="thread-a",
            turn_id="turn-1",
            memory=memory,
            ingest=True,
            message_id="msg-ambiguous",
        )
        loaded = await backend.history().load("thread-a")
        cursor = await backend._conn.execute(  # type: ignore[union-attr]
            "SELECT content FROM memory_outbox"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    finally:
        await backend.close()
    assert history.calls == 2
    assert len(loaded) == 1
    assert [row[0] for row in rows] == ["persist me"]
