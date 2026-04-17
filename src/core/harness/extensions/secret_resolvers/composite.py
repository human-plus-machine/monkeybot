"""Composite :class:`SecretResolver` chaining multiple backends (Story 6).

Iterates the chain in order and returns the first resolver that successfully
returns a value. A leg raising :class:`SecretNotFound` is treated as a miss
and the iteration continues; any other exception short-circuits and
propagates (network / auth failures must not be silently swallowed). If every
leg raises :class:`SecretNotFound`, the composite raises
:class:`SecretNotFound` for the requested handle.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import SecretStr

from ..base import SecretResolver
from ..errors import SecretNotFound


class CompositeSecretResolver(SecretResolver):
    """First-match-wins chain of :class:`SecretResolver` instances.

    Args:
        chain: Ordered resolvers. Order is significant — earlier entries win
            on successful resolve.
    """

    def __init__(self, chain: Sequence[SecretResolver]) -> None:
        self.chain: tuple[SecretResolver, ...] = tuple(chain)

    async def resolve(self, handle: str) -> SecretStr:
        """Return the first resolver's ``SecretStr`` for ``handle``.

        Raises:
            SecretNotFound: Every leg raised :class:`SecretNotFound`.
        """
        for leg in self.chain:
            try:
                return await leg.resolve(handle)
            except SecretNotFound:
                continue
        raise SecretNotFound(handle)


__all__ = ["CompositeSecretResolver"]
