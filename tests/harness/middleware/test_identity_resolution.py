"""Tests for :class:`IdentityResolutionMW` (Story 5).

Covers the override path, cache hit/miss, retry-on-transient-error, and
``IdentityNotFound`` propagation with no retry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.core.harness.events import Principal
from src.core.harness.extensions import CallableIdentitySource, IdentityNotFound
from src.core.harness.extensions.errors import BackendConfigError
from src.core.harness.extensions.values import LoadedIdentity
from src.core.harness.middleware.identity_resolution import IdentityResolutionMW

pytestmark = pytest.mark.asyncio


def _identity(principal_id: str, ttl_seconds: int = 60) -> LoadedIdentity:
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
        ttl_seconds=ttl_seconds,
        source_backend="callable",
    )


class _CountingSource:
    """Minimal :class:`IdentitySource` stand-in that records call counts."""

    def __init__(self) -> None:
        self.calls = 0
        self.raise_on_first: BaseException | None = None

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,  # noqa: ARG002
    ) -> LoadedIdentity:
        self.calls += 1
        if self.raise_on_first is not None and self.calls == 1:
            exc = self.raise_on_first
            self.raise_on_first = None
            raise exc
        return _identity(principal.id)

    async def write_memory(self, **_: Any) -> None:
        raise NotImplementedError


async def test_mw_override_bypasses_source() -> None:
    """When ``ctx['identity_override']`` is set, the source is never called."""

    async def fn(principal: Principal, _: str | None) -> LoadedIdentity:
        raise AssertionError("source must not be called when override is present")

    mw = IdentityResolutionMW(CallableIdentitySource(fn))
    override = _identity("alice")
    ctx: dict[str, Any] = {
        "principal": Principal(kind="user", id="alice"),
        "session_id": "s1",
        "identity_override": override,
    }
    await mw.before(state=None, ctx=ctx)
    assert ctx["identity"] is override


async def test_mw_cache_hit_returns_without_calling_source() -> None:
    """Second invocation serves from the cache and never re-calls the source."""
    source = _CountingSource()
    mw = IdentityResolutionMW(source)
    principal = Principal(kind="user", id="alice")
    ctx: dict[str, Any] = {"principal": principal, "session_id": "s1"}

    await mw.before(state=None, ctx=ctx)
    assert source.calls == 1
    ctx2: dict[str, Any] = {"principal": principal, "session_id": "s1"}
    await mw.before(state=None, ctx=ctx2)
    assert source.calls == 1  # hit
    stats = mw.cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


async def test_mw_retries_on_transient_backend_error() -> None:
    """A single :class:`BackendConfigError` is retried and eventually succeeds."""
    source = _CountingSource()
    source.raise_on_first = BackendConfigError("transient")
    mw = IdentityResolutionMW(source)
    ctx: dict[str, Any] = {
        "principal": Principal(kind="user", id="alice"),
        "session_id": "s1",
    }
    await mw.before(state=None, ctx=ctx)
    assert source.calls == 2
    assert ctx["identity"].principal_id == "alice"


async def test_mw_retries_on_asyncio_timeout() -> None:
    """``asyncio.TimeoutError`` also triggers the single retry path."""
    source = _CountingSource()
    source.raise_on_first = TimeoutError()
    mw = IdentityResolutionMW(source)
    ctx: dict[str, Any] = {
        "principal": Principal(kind="user", id="alice"),
        "session_id": None,
    }
    await mw.before(state=None, ctx=ctx)
    assert source.calls == 2


async def test_mw_identity_not_found_is_not_retried() -> None:
    """:class:`IdentityNotFound` propagates on the first failure."""

    class AlwaysMissing:
        async def load(self, **_: Any) -> LoadedIdentity:
            raise IdentityNotFound("missing")

        async def write_memory(self, **_: Any) -> None:
            raise NotImplementedError

    mw = IdentityResolutionMW(AlwaysMissing())  # type: ignore[arg-type]
    ctx: dict[str, Any] = {
        "principal": Principal(kind="user", id="nobody"),
        "session_id": None,
    }
    with pytest.raises(IdentityNotFound):
        await mw.before(state=None, ctx=ctx)


async def test_mw_clamps_ttl_to_default() -> None:
    """``default_ttl_seconds`` is an upper bound even if the identity claims more."""

    async def fn(principal: Principal, _: str | None) -> LoadedIdentity:
        return _identity(principal.id, ttl_seconds=10_000)

    mw = IdentityResolutionMW(CallableIdentitySource(fn), default_ttl_seconds=5)
    ctx: dict[str, Any] = {
        "principal": Principal(kind="user", id="alice"),
        "session_id": None,
    }
    await mw.before(state=None, ctx=ctx)
    await mw.before(state=None, ctx=dict(ctx))
    assert mw.cache.stats()["hits"] == 1
