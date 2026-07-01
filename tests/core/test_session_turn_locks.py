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
    await backend.close()


@pytest.mark.asyncio
async def test_session_turn_lock_stale_claim_released(tmp_path) -> None:
    import asyncio

    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'stale.db'}")
    await backend.open()
    locks = backend.session_turns()
    assert await locks.try_acquire("sess-a", "req-1")
    await asyncio.sleep(0.01)
    released = await locks.release_stale_claims(1)
    assert released == 1
    assert not await locks.is_busy("sess-a")
    assert await locks.try_acquire("sess-a", "req-2")
    await backend.close()
