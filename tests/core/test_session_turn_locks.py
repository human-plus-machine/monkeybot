"""Tests for durable session turn locks."""

from __future__ import annotations

import pytest

from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend


@pytest.mark.asyncio
async def test_session_turn_lock_exclusive(tmp_path) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'locks.db'}")
    await backend.open()
    locks = backend.session_turns()
    assert await locks.try_acquire("sess-a", "req-1")
    assert await locks.is_busy("sess-a")
    assert not await locks.try_acquire("sess-a", "req-2")
    await locks.release("sess-a", "req-1")
    assert not await locks.is_busy("sess-a")
    assert await locks.try_acquire("sess-a", "req-2")
    await backend.close()
