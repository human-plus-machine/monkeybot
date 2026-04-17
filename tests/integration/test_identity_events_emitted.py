"""Phase 6 integration test — IDENTITY_* events flow through the EventBus.

Proves the fix for Phase 5 verifier finding:

    "IDENTITY_LOAD/CACHE_EVICT/BUST declared but NEVER published"

The middleware is now handed the shared :class:`EventBus` at assembly time
and emits real :class:`HarnessEvent` instances for every identity
transition (load / cache_evict / bust).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from src.core.harness.event_bus import EventBus
from src.core.harness.events import EventKind, HarnessEvent, Principal, VersionTriple
from src.core.harness.extensions.base import IdentitySource
from src.core.harness.extensions.values import LoadedIdentity, MemoryPatch
from src.core.harness.middleware.identity_resolution import IdentityResolutionMW


class _RecordingHandler:
    """Event bus handler that captures every :class:`HarnessEvent` for assertion."""

    name = "RecordingHandler"

    def __init__(self) -> None:
        self.events: list[HarnessEvent] = []

    async def handle(self, event: HarnessEvent) -> None:
        self.events.append(event)


class _FakeIdentitySource(IdentitySource):
    """Deterministic in-process IdentitySource used by the tests."""

    def __init__(self) -> None:
        self.load_calls = 0

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        self.load_calls += 1
        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul="soul",
            loaded_at=datetime.now(UTC),
            ttl_seconds=60,
            source_backend="fake",
            extras={},
        )

    async def write_memory(
        self, *, principal: Principal, patch: MemoryPatch
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_identity_load_emits_identity_load_event() -> None:
    """Cold miss → backend load → ``IDENTITY_LOAD`` published on the bus."""
    bus = EventBus(include_default_logger=False)
    recorder = _RecordingHandler()
    bus.subscribe(recorder)

    mw = IdentityResolutionMW(
        _FakeIdentitySource(),
        event_bus=bus,
        versions=VersionTriple(harness="1", deep_agents="test", model="test"),
    )
    principal = Principal(kind="user", id="alice")
    ctx: dict = {"principal": principal, "session_id": "sess-1", "run_id": "run-1"}

    await mw.before(state={}, ctx=ctx)
    # Bus delivery is fire-and-forget via loop.create_task; let scheduled tasks drain.
    for _ in range(10):
        await asyncio.sleep(0)

    load_events = [e for e in recorder.events if e.kind == EventKind.IDENTITY_LOAD]
    assert len(load_events) == 1, (
        f"expected one IDENTITY_LOAD, got kinds={[e.kind.value for e in recorder.events]}"
    )
    payload: Mapping[str, object] = load_events[0].payload
    assert payload["cache_hit"] is False
    assert payload["principal_id"] == "alice"


@pytest.mark.asyncio
async def test_cache_bust_emits_identity_bust_event() -> None:
    """Invalidating a cache entry fires ``IDENTITY_BUST`` on the bus."""
    bus = EventBus(include_default_logger=False)
    recorder = _RecordingHandler()
    bus.subscribe(recorder)

    mw = IdentityResolutionMW(
        _FakeIdentitySource(),
        event_bus=bus,
        versions=VersionTriple(harness="1", deep_agents="test", model="test"),
    )
    principal = Principal(kind="user", id="bob")
    ctx: dict = {"principal": principal, "session_id": "sess-2", "run_id": "run-2"}

    await mw.before(state={}, ctx=ctx)
    for _ in range(10):
        await asyncio.sleep(0)
    recorder.events.clear()

    count = mw.cache.invalidate(lambda key: key[0] == "bob")
    for _ in range(10):
        await asyncio.sleep(0)

    assert count == 1
    bust_events = [e for e in recorder.events if e.kind == EventKind.IDENTITY_BUST]
    assert len(bust_events) == 1, (
        f"expected one IDENTITY_BUST, got {[e.kind.value for e in recorder.events]}"
    )
    assert bust_events[0].payload["reason"] == "bust"
    assert bust_events[0].payload["principal_id"] == "bob"


@pytest.mark.asyncio
async def test_identity_mw_without_bus_falls_back_to_logger() -> None:
    """When no bus is wired the middleware degrades gracefully."""
    mw = IdentityResolutionMW(_FakeIdentitySource())
    principal = Principal(kind="user", id="carol")
    ctx: dict = {"principal": principal}

    await mw.before(state={}, ctx=ctx)
