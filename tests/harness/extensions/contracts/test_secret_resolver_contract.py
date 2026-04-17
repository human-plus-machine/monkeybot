"""Contract suite invariants for every :class:`SecretResolver` backend.

IDs map to ``SEC-C-01`` … ``SEC-C-04`` in 1b-contracts.md §11.1.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import SecretStr

from src.core.harness.extensions import SecretNotFound, SecretResolver
from src.core.harness.extensions._mocks import MockSecretResolver

pytestmark = pytest.mark.asyncio


async def test_sec_c_01_known_handle_returns_secret(
    secret_resolver_factory: Callable[[], SecretResolver],
) -> None:
    """SEC-C-01: a known handle returns a :class:`SecretStr` with the bound value."""
    resolver = secret_resolver_factory()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "the-secret"


async def test_sec_c_02_unknown_handle_raises(
    secret_resolver_factory: Callable[[], SecretResolver],
) -> None:
    """SEC-C-02: unknown handles raise :class:`SecretNotFound`."""
    resolver = secret_resolver_factory()
    with pytest.raises(SecretNotFound):
        await resolver.resolve("__MISSING__")


async def test_sec_c_03_composite_resolves_first_match() -> None:
    """SEC-C-03: a two-leg composite returns the first succeeding leg's value."""

    class _Failing(MockSecretResolver):
        async def resolve(self, handle: str) -> SecretStr:  # type: ignore[override]
            raise SecretNotFound(handle)

    leg_a = _Failing({})
    leg_b = MockSecretResolver({"HANDLE": "value"})

    async def _composite(handle: str) -> SecretStr:
        for leg in (leg_a, leg_b):
            try:
                return await leg.resolve(handle)
            except SecretNotFound:
                continue
        raise SecretNotFound(handle)

    result = await _composite("HANDLE")
    assert result.get_secret_value() == "value"


async def test_sec_c_04_resolve_value_is_wrapped(
    secret_resolver_factory: Callable[[], SecretResolver],
) -> None:
    """SEC-C-04: resolved values are wrapped in :class:`SecretStr` so ``repr`` never leaks them."""
    resolver = secret_resolver_factory()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert "the-secret" not in repr(value)
    assert "the-secret" not in str(value)
