"""Tests for :class:`CompositeSecretResolver` (Story 6).

Covers the first-match-wins invariant (SEC-C-03) and the all-fail path
that surfaces :class:`SecretNotFound`. Legs are built out of ``MagicMock``
so test assertions can verify which legs were called.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from src.core.harness.extensions import SecretNotFound
from src.core.harness.extensions.secret_resolvers import CompositeSecretResolver

pytestmark = pytest.mark.asyncio


def _leg_returning(value: str) -> MagicMock:
    leg = MagicMock()
    leg.resolve = AsyncMock(return_value=SecretStr(value))
    return leg


def _leg_missing() -> MagicMock:
    leg = MagicMock()
    leg.resolve = AsyncMock(side_effect=SecretNotFound("HANDLE"))
    return leg


def _leg_raising(exc: Exception) -> MagicMock:
    leg = MagicMock()
    leg.resolve = AsyncMock(side_effect=exc)
    return leg


async def test_first_match_wins_short_circuits_remaining_legs() -> None:
    """SEC-C-03: first successful leg wins; later legs must not be invoked."""
    leg_a = _leg_missing()
    leg_b = _leg_returning("value")
    leg_c = _leg_returning("never-reached")
    composite = CompositeSecretResolver(chain=[leg_a, leg_b, leg_c])

    result = await composite.resolve("HANDLE")

    assert result.get_secret_value() == "value"
    leg_a.resolve.assert_awaited_once_with("HANDLE")
    leg_b.resolve.assert_awaited_once_with("HANDLE")
    leg_c.resolve.assert_not_awaited()


async def test_all_legs_missing_raises_secret_not_found() -> None:
    """Every leg raising :class:`SecretNotFound` surfaces :class:`SecretNotFound`."""
    legs = [_leg_missing(), _leg_missing(), _leg_missing()]
    composite = CompositeSecretResolver(chain=legs)
    with pytest.raises(SecretNotFound) as excinfo:
        await composite.resolve("HANDLE")
    assert excinfo.value.handle == "HANDLE"
    for leg in legs:
        leg.resolve.assert_awaited_once_with("HANDLE")


async def test_non_not_found_errors_propagate_and_stop_chain() -> None:
    """Unexpected errors short-circuit the chain (do not silently continue)."""
    leg_a = _leg_raising(RuntimeError("boom"))
    leg_b = _leg_returning("should-not-be-called")
    composite = CompositeSecretResolver(chain=[leg_a, leg_b])

    with pytest.raises(RuntimeError):
        await composite.resolve("HANDLE")

    leg_a.resolve.assert_awaited_once_with("HANDLE")
    leg_b.resolve.assert_not_awaited()


async def test_empty_chain_raises_not_found() -> None:
    """A composite with an empty chain immediately raises :class:`SecretNotFound`."""
    composite = CompositeSecretResolver(chain=[])
    with pytest.raises(SecretNotFound):
        await composite.resolve("HANDLE")


async def test_chain_is_materialised_as_tuple() -> None:
    """The composite stores ``chain`` as an immutable tuple so leg order is stable."""
    leg = _leg_returning("v")
    composite = CompositeSecretResolver(chain=[leg])
    assert isinstance(composite.chain, tuple)
    assert composite.chain == (leg,)
