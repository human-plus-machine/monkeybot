"""Contract suite invariants for every :class:`IdentitySource` backend.

IDs map to ``ID-C-01`` … ``ID-C-05`` in 1b-contracts.md §11.1.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.core.harness.events import Principal
from src.core.harness.extensions import IdentityNotFound, IdentitySource
from src.core.harness.extensions.values import MemoryPatch

pytestmark = pytest.mark.asyncio


def _principal(name: str = "alice") -> Principal:
    return Principal(kind="user", id=name)


async def test_id_c_01_load_known_principal(
    identity_source_factory: Callable[[], IdentitySource],
) -> None:
    """ID-C-01: ``load`` returns a populated :class:`LoadedIdentity`."""
    source = identity_source_factory()
    identity = await source.load(principal=_principal("alice"))
    assert identity.principal_id == "alice"
    assert identity.soul
    assert identity.rules


async def test_id_c_02_unknown_principal_raises(
    identity_source_factory: Callable[[], IdentitySource],
) -> None:
    """ID-C-02: unknown principals raise :class:`IdentityNotFound`."""
    source = identity_source_factory()
    with pytest.raises(IdentityNotFound):
        await source.load(principal=_principal("nobody"))


async def test_id_c_03_write_memory_round_trip(
    identity_source_factory: Callable[[], IdentitySource],
) -> None:
    """ID-C-03: ``write_memory`` updates are visible on the next ``load``."""
    source = identity_source_factory()
    principal = _principal("alice")
    try:
        await source.write_memory(
            principal=principal,
            patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
        )
    except NotImplementedError:
        pytest.skip("backend does not support write_memory")
    identity = await source.load(principal=principal)
    assert "hello" in identity.memory


async def test_id_c_04_load_is_idempotent(
    identity_source_factory: Callable[[], IdentitySource],
) -> None:
    """ID-C-04: repeated loads return equivalent identity projections."""
    source = identity_source_factory()
    principal = _principal("alice")
    first = await source.load(principal=principal, session_id="s1")
    second = await source.load(principal=principal, session_id="s1")
    assert first.principal_id == second.principal_id
    assert first.soul == second.soul
    assert first.rules == second.rules


async def test_id_c_05_callable_source_delegates(
    identity_source_factory: Callable[[], IdentitySource],
) -> None:
    """ID-C-05: ``CallableIdentitySource`` skips backend wiring (Story 1 mock is a stand-in)."""
    source = identity_source_factory()
    identity = await source.load(principal=_principal("bob"))
    assert identity.principal_id == "bob"
    assert identity.source_backend
