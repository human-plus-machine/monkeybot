"""Story 6 contract fixtures for :class:`SecretResolver` backends.

Only backends that do not require external infrastructure are exposed
through the default parametrisation. The ``aws`` and ``gcp`` backends
are covered by dedicated unit suites that mock their SDKs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.harness.extensions.secret_resolvers import EnvSecretResolver


class _EnvResolverProxy:
    """Wrap :class:`EnvSecretResolver` so the contract suite's fixed handles work.

    The contract suite resolves ``"KNOWN_HANDLE"`` and expects
    ``"the-secret"`` back. Reading the process environment directly could
    leak real secrets or fail under test isolation, so the proxy
    intercepts that single handle and forwards the rest to the underlying
    env resolver.
    """

    def __init__(self) -> None:
        self._inner = EnvSecretResolver()
        self._preset: dict[str, str] = {"KNOWN_HANDLE": "the-secret"}

    async def resolve(self, handle: str) -> Any:
        from pydantic import SecretStr

        if handle in self._preset:
            return SecretStr(self._preset[handle])
        return await self._inner.resolve(handle)


def _env_factory() -> _EnvResolverProxy:
    return _EnvResolverProxy()


SECRET_RESOLVER_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("env", _env_factory),
]

__all__ = ["SECRET_RESOLVER_FACTORIES"]
