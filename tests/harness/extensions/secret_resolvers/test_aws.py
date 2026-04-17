"""SEC-C-01 … SEC-C-04 for :class:`AWSSecretsManagerResolver`.

``aioboto3`` is an optional dependency, so the real client is substituted
with a lightweight fake that mimics the async-context-manager surface used
by the resolver. Tests patch
:func:`src.core.harness.extensions.secret_resolvers.aws.secrets_client`
which is the single integration point with the AWS SDK.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from src.core.harness.extensions import SecretNotFound, SecretResolverError
from src.core.harness.extensions.secret_resolvers import AWSSecretsManagerResolver
from src.core.harness.extensions.secret_resolvers import aws as aws_module

pytestmark = pytest.mark.asyncio


class _FakeClientError(Exception):
    """Mimics ``botocore.exceptions.ClientError`` surface used by the resolver."""

    def __init__(self, code: str, message: str = "error") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class _FakeSecretsClient:
    """Minimal async-context-manager Secrets Manager stand-in."""

    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeSecretsClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803 - AWS kwarg
        self.calls.append(SecretId)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap ``aws_module.secrets_client`` for a factory returning a fake client."""

    def _install(fake: _FakeSecretsClient) -> _FakeSecretsClient:
        def _factory(region: str | None = None) -> _FakeSecretsClient:
            return fake

        monkeypatch.setattr(aws_module, "secrets_client", _factory)
        return fake

    return _install


async def test_sec_c_01_known_handle_returns_secret(patch_client: Any) -> None:
    """SEC-C-01: GetSecretValue happy path returns a wrapped :class:`SecretStr`."""
    patch_client(_FakeSecretsClient(response={"SecretString": "the-secret"}))
    resolver = AWSSecretsManagerResolver()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "the-secret"


async def test_sec_c_02_resource_not_found_raises(patch_client: Any) -> None:
    """SEC-C-02: ``ResourceNotFoundException`` maps to :class:`SecretNotFound`."""
    patch_client(
        _FakeSecretsClient(error=_FakeClientError("ResourceNotFoundException"))
    )
    resolver = AWSSecretsManagerResolver()
    with pytest.raises(SecretNotFound):
        await resolver.resolve("__MISSING__")


async def test_sec_c_04_resolve_value_is_wrapped(patch_client: Any) -> None:
    """SEC-C-04: repr/str on the returned ``SecretStr`` never leaks the value."""
    patch_client(_FakeSecretsClient(response={"SecretString": "the-secret"}))
    resolver = AWSSecretsManagerResolver()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert "the-secret" not in repr(value)
    assert "the-secret" not in str(value)


async def test_cache_hits_skip_the_client(patch_client: Any) -> None:
    """Second resolve of the same handle must hit the cache (no client call)."""
    fake = patch_client(_FakeSecretsClient(response={"SecretString": "cached"}))
    resolver = AWSSecretsManagerResolver(cache_ttl_seconds=60, cache_capacity=4)
    first = await resolver.resolve("HANDLE")
    second = await resolver.resolve("HANDLE")
    assert first.get_secret_value() == second.get_secret_value() == "cached"
    assert fake.calls == ["HANDLE"]


async def test_binary_payload_is_decoded(patch_client: Any) -> None:
    """``SecretBinary`` payloads are UTF-8 decoded before wrapping in ``SecretStr``."""
    patch_client(_FakeSecretsClient(response={"SecretBinary": b"binary-secret"}))
    resolver = AWSSecretsManagerResolver()
    value = await resolver.resolve("BIN")
    assert value.get_secret_value() == "binary-secret"


async def test_throttling_is_transient(patch_client: Any) -> None:
    """``ThrottlingException`` surfaces as :class:`SecretResolverError` (transient)."""
    patch_client(_FakeSecretsClient(error=_FakeClientError("ThrottlingException")))
    resolver = AWSSecretsManagerResolver()
    with pytest.raises(SecretResolverError):
        await resolver.resolve("HANDLE")


async def test_connection_error_wraps_as_resolver_error(patch_client: Any) -> None:
    """Transport errors surface as :class:`SecretResolverError`, not raw ``ConnectionError``."""
    patch_client(_FakeSecretsClient(error=ConnectionError("boom")))
    resolver = AWSSecretsManagerResolver()
    with pytest.raises(SecretResolverError):
        await resolver.resolve("HANDLE")
