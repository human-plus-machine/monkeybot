"""Tests for :class:`CallableIdentitySource` (Story 5).

Exercise the happy path, timeout translation, and ``write_memory``
refusal. Runs without any optional cloud SDK so never skips.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from src.core.harness.events import Principal
from src.core.harness.extensions import CallableIdentitySource, IdentityNotFound
from src.core.harness.extensions.values import LoadedIdentity, MemoryPatch

pytestmark = pytest.mark.asyncio


def _identity(principal_id: str) -> LoadedIdentity:
    return LoadedIdentity(
        principal_id=principal_id,
        soul="s",
        rules="r",
        identity="i",
        user="u",
        index="ix",
        memory="m",
        heartbeat="h",
        loaded_at=datetime.now(UTC),
        ttl_seconds=60,
        source_backend="callable",
    )


async def test_callable_load_returns_identity() -> None:
    """Happy path: the wrapped callable's return value is passed through."""

    async def fn(principal: Principal, _session_id: str | None) -> LoadedIdentity:
        return _identity(principal.id)

    source = CallableIdentitySource(fn)
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.principal_id == "alice"
    assert identity.source_backend == "callable"


async def test_callable_timeout_raises_identity_not_found() -> None:
    """``asyncio.TimeoutError`` is translated into :class:`IdentityNotFound`."""

    async def slow(_principal: Principal, _session_id: str | None) -> LoadedIdentity:
        await asyncio.sleep(0.2)
        return _identity("alice")

    source = CallableIdentitySource(slow, timeout_seconds=0.01)
    with pytest.raises(IdentityNotFound):
        await source.load(principal=Principal(kind="user", id="alice"))


async def test_callable_write_memory_raises_notimplemented() -> None:
    """``write_memory`` must refuse: callables are read-only."""

    async def fn(principal: Principal, _session_id: str | None) -> LoadedIdentity:
        return _identity(principal.id)

    source = CallableIdentitySource(fn)
    with pytest.raises(NotImplementedError):
        await source.write_memory(
            principal=Principal(kind="user", id="alice"),
            patch=MemoryPatch(target="MEMORY.md", operation="replace", content="x"),
        )


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_callable_rejects_non_callable() -> None:
    """Constructor refuses non-callable ``fn``."""
    with pytest.raises(TypeError):
        CallableIdentitySource("not a function")  # type: ignore[arg-type]
