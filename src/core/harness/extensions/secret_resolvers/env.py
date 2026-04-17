"""Environment variable-backed :class:`SecretResolver` (Story 6).

Reads ``os.environ[f"{prefix}{handle}"]`` and wraps the result in
:class:`pydantic.SecretStr`. Raises :class:`SecretNotFound` when the key is
absent. No caching layer is needed because ``os.environ`` reads are already
O(1) and in-process.
"""

from __future__ import annotations

import os

from pydantic import SecretStr

from ..base import SecretResolver
from ..errors import SecretNotFound


class EnvSecretResolver(SecretResolver):
    """Resolve secret handles against ``os.environ``.

    Args:
        prefix: Optional string prepended to every handle before the
            environment lookup (for example ``prefix="EMONK_"`` makes
            ``resolve("DB_PASS")`` read ``EMONK_DB_PASS``).
    """

    def __init__(self, *, prefix: str = "") -> None:
        self.prefix = prefix

    async def resolve(self, handle: str) -> SecretStr:
        """Return ``SecretStr`` bound to ``{prefix}{handle}``.

        Raises:
            SecretNotFound: The composed env var is not set.
        """
        env_key = f"{self.prefix}{handle}"
        try:
            value = os.environ[env_key]
        except KeyError as exc:
            raise SecretNotFound(handle) from exc
        return SecretStr(value)


__all__ = ["EnvSecretResolver"]
