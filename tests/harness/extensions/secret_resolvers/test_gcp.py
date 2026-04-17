"""SEC-C-01 … SEC-C-04 for :class:`GCPSecretManagerResolver`.

The real ``google.cloud.secretmanager`` client is replaced with a mock that
mimics the ``access_secret_version`` surface used by the resolver. The
mock lets us exercise happy-path, not-found, and transport failure modes
without needing the optional dependency installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import SecretStr

from src.core.harness.extensions import SecretNotFound, SecretResolverError
from src.core.harness.extensions.secret_resolvers import GCPSecretManagerResolver

pytestmark = pytest.mark.asyncio


@dataclass
class _FakePayload:
    data: bytes


@dataclass
class _FakeResponse:
    payload: _FakePayload


class _FakeGCPClient:
    """Minimal stand-in for ``SecretManagerServiceClient``."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[str] = []

    def access_secret_version(self, *, request: dict[str, Any]) -> _FakeResponse:
        self.calls.append(str(request.get("name", "")))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class _FakeNotFound(Exception):  # noqa: N818 - intentionally mirrors google.api_core.exceptions.NotFound
    """Stand-in for ``google.api_core.exceptions.NotFound`` (name-matched)."""

    def __init__(self, message: str = "not found") -> None:
        super().__init__(message)


_FakeNotFound.__name__ = "NotFound"


async def test_sec_c_01_known_handle_returns_secret() -> None:
    """SEC-C-01: happy path returns a :class:`SecretStr` with the payload value."""
    client = _FakeGCPClient(response=_FakeResponse(_FakePayload(b"the-secret")))
    resolver = GCPSecretManagerResolver(project_id="proj", client=client)
    value = await resolver.resolve("KNOWN_HANDLE")
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "the-secret"
    assert client.calls == ["projects/proj/secrets/KNOWN_HANDLE/versions/latest"]


async def test_sec_c_02_not_found_raises() -> None:
    """SEC-C-02: ``NotFound`` from the SDK maps to :class:`SecretNotFound`."""
    client = _FakeGCPClient(error=_FakeNotFound("secret not found"))
    resolver = GCPSecretManagerResolver(project_id="proj", client=client)
    with pytest.raises(SecretNotFound):
        await resolver.resolve("__MISSING__")


async def test_sec_c_04_resolve_value_is_wrapped() -> None:
    """SEC-C-04: ``SecretStr`` hides the value from repr/str."""
    client = _FakeGCPClient(response=_FakeResponse(_FakePayload(b"the-secret")))
    resolver = GCPSecretManagerResolver(project_id="proj", client=client)
    value = await resolver.resolve("KNOWN_HANDLE")
    assert "the-secret" not in repr(value)
    assert "the-secret" not in str(value)


async def test_cache_hits_skip_the_client() -> None:
    """Second resolve of the same handle must hit the cache (no client call)."""
    client = _FakeGCPClient(response=_FakeResponse(_FakePayload(b"cached")))
    resolver = GCPSecretManagerResolver(
        project_id="proj", client=client, cache_ttl_seconds=60, cache_capacity=4
    )
    await resolver.resolve("HANDLE")
    await resolver.resolve("HANDLE")
    assert client.calls == ["projects/proj/secrets/HANDLE/versions/latest"]


async def test_transport_failure_wraps_as_resolver_error() -> None:
    """Non-NotFound exceptions surface as :class:`SecretResolverError`."""
    client = _FakeGCPClient(error=RuntimeError("transient gRPC"))
    resolver = GCPSecretManagerResolver(project_id="proj", client=client)
    with pytest.raises(SecretResolverError):
        await resolver.resolve("HANDLE")


async def test_empty_project_id_is_rejected() -> None:
    """An empty ``project_id`` raises :class:`ValueError` at construction time."""
    with pytest.raises(ValueError):
        GCPSecretManagerResolver(project_id="")
