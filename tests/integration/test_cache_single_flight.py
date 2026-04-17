"""Phase 6 integration test — R-16 single-flight protection.

Concurrent cold-miss lookups for the same ``(principal_id, session_id)``
collapse to a single backend ``load`` call. This protects the identity
source from thundering-herd load on cache miss / startup.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from src.core.harness.events import Principal, VersionTriple
from src.core.harness.extensions.base import IdentitySource
from src.core.harness.extensions.values import LoadedIdentity, MemoryPatch
from src.core.harness.middleware.identity_resolution import IdentityResolutionMW


class _SlowCountingSource(IdentitySource):
    """Records every ``load`` call and blocks on a gate to force overlap."""

    def __init__(self, release: asyncio.Event) -> None:
        self.calls = 0
        self._release = release

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        self.calls += 1
        await self._release.wait()
        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul="soul",
            loaded_at=datetime.now(UTC),
            ttl_seconds=60,
            source_backend="slow",
            extras={},
        )

    async def write_memory(self, *, principal: Principal, patch: MemoryPatch) -> None:
        return None


@pytest.mark.asyncio
async def test_concurrent_cold_misses_collapse_to_single_backend_load() -> None:
    """20 parallel cold-miss requests → exactly one ``load`` call."""
    release = asyncio.Event()
    source = _SlowCountingSource(release)
    mw = IdentityResolutionMW(
        source,
        versions=VersionTriple(harness="1", deep_agents="test", model="test"),
    )
    principal = Principal(kind="user", id="zoe")

    async def _one_call() -> None:
        ctx: dict = {"principal": principal, "session_id": "sess"}
        await mw.before(state={}, ctx=ctx)

    tasks = [asyncio.create_task(_one_call()) for _ in range(20)]
    # Give all tasks a chance to enqueue before releasing the gate.
    for _ in range(5):
        await asyncio.sleep(0)

    release.set()
    await asyncio.gather(*tasks)

    assert source.calls == 1, (
        f"single-flight collapsed parallel cold misses to 1 load; got {source.calls}"
    )


@pytest.mark.asyncio
async def test_subsequent_request_after_cache_warm_hits_cache() -> None:
    """After the first load completes, the next request is a cache hit."""
    release = asyncio.Event()
    release.set()
    source = _SlowCountingSource(release)
    mw = IdentityResolutionMW(
        source,
        versions=VersionTriple(harness="1", deep_agents="test", model="test"),
    )
    principal = Principal(kind="user", id="ivy")

    ctx1: dict = {"principal": principal, "session_id": "sess"}
    await mw.before(state={}, ctx=ctx1)
    ctx2: dict = {"principal": principal, "session_id": "sess"}
    await mw.before(state={}, ctx=ctx2)

    assert source.calls == 1, (
        f"expected single load + cache hit; got {source.calls} backend calls"
    )
