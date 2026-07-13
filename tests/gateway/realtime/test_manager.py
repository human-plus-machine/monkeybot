"""Tests for the realtime session manager and concurrency guardrails."""

from __future__ import annotations

import pytest

from monkeybot.core.config.realtime_config import RealtimeConfig, RealtimeSessionConfig
from monkeybot.gateway.realtime.manager import RealtimeSessionManager


def _make_config(max_sessions: int = 2) -> RealtimeConfig:
    return RealtimeConfig(
        session=RealtimeSessionConfig(max_concurrent_sessions=max_sessions),
    )


@pytest.mark.asyncio
async def test_acquire_slot_grants_when_under_limit() -> None:
    manager = RealtimeSessionManager(_make_config(2))
    assert await manager.acquire_slot("s1") is True
    assert await manager.acquire_slot("s2") is True


@pytest.mark.asyncio
async def test_acquire_slot_rejects_at_limit() -> None:
    manager = RealtimeSessionManager(_make_config(1))
    assert await manager.acquire_slot("s1") is True
    assert await manager.acquire_slot("s2") is False


@pytest.mark.asyncio
async def test_release_slot_allows_new_sessions() -> None:
    manager = RealtimeSessionManager(_make_config(1))
    assert await manager.acquire_slot("s1") is True
    manager.release_slot("s1")
    assert await manager.acquire_slot("s2") is True


@pytest.mark.asyncio
async def test_register_rejects_duplicate_active_session() -> None:
    manager = RealtimeSessionManager(_make_config(5))
    await manager.acquire_slot("s1")
    first = object()
    second = object()
    manager.register("s1", first)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already active"):
        manager.register("s1", second)  # type: ignore[arg-type]
    assert manager.get("s1") is first


@pytest.mark.asyncio
async def test_remove_only_drops_matching_connection() -> None:
    manager = RealtimeSessionManager(_make_config(5))
    await manager.acquire_slot("s1")
    first = object()
    second = object()
    manager.register("s1", first)  # type: ignore[arg-type]
    # Stale close for a replaced connection must not clear the live entry.
    manager.remove("s1", second)  # type: ignore[arg-type]
    assert manager.get("s1") is first
    manager.remove("s1", first)  # type: ignore[arg-type]
    assert manager.get("s1") is None


@pytest.mark.asyncio
async def test_register_and_remove_sessions() -> None:
    manager = RealtimeSessionManager(_make_config(5))
    await manager.acquire_slot("s1")
    state = object()
    manager.register("s1", state)  # type: ignore[arg-type]
    assert manager.get("s1") is state
    manager.remove("s1")
    assert manager.get("s1") is None


def test_snapshot_metrics() -> None:
    manager = RealtimeSessionManager(_make_config(10))
    assert manager.snapshot_metrics() == {
        "active_sessions": 0,
        "max_concurrent_sessions": 10,
    }


@pytest.mark.asyncio
async def test_concurrent_acquire_respects_limit() -> None:
    manager = RealtimeSessionManager(_make_config(1))
    first = await manager.acquire_slot("s1")
    second = await manager.acquire_slot("s2")
    assert first is True
    assert second is False
