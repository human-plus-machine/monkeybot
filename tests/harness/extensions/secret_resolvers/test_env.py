"""SEC-C-01 … SEC-C-04 for :class:`EnvSecretResolver`.

The env backend reads ``os.environ[f"{prefix}{handle}"]`` and wraps the
value in :class:`pydantic.SecretStr`. Tests use ``monkeypatch.setenv`` so
the surrounding process environment is never mutated.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.core.harness.extensions import SecretNotFound
from src.core.harness.extensions.secret_resolvers import EnvSecretResolver

pytestmark = pytest.mark.asyncio


async def test_sec_c_01_known_handle_returns_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-C-01: a known handle returns a :class:`SecretStr` with the bound value."""
    monkeypatch.setenv("KNOWN_HANDLE", "the-secret")
    resolver = EnvSecretResolver()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "the-secret"


async def test_sec_c_02_unknown_handle_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-C-02: unknown handles raise :class:`SecretNotFound`."""
    monkeypatch.delenv("__MISSING__", raising=False)
    resolver = EnvSecretResolver()
    with pytest.raises(SecretNotFound):
        await resolver.resolve("__MISSING__")


async def test_sec_c_04_resolve_value_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-C-04: resolved values are wrapped in :class:`SecretStr` so ``repr`` never leaks them."""
    monkeypatch.setenv("KNOWN_HANDLE", "the-secret")
    resolver = EnvSecretResolver()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert "the-secret" not in repr(value)
    assert "the-secret" not in str(value)


async def test_prefix_applied_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prefix`` is prepended to the handle before the env lookup."""
    monkeypatch.setenv("EMONK_DB_PASS", "pw-1")
    resolver = EnvSecretResolver(prefix="EMONK_")
    value = await resolver.resolve("DB_PASS")
    assert value.get_secret_value() == "pw-1"


async def test_prefix_missing_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefix miss still surfaces as :class:`SecretNotFound`."""
    monkeypatch.delenv("EMONK_NOPE", raising=False)
    resolver = EnvSecretResolver(prefix="EMONK_")
    with pytest.raises(SecretNotFound):
        await resolver.resolve("NOPE")


async def test_sec_not_found_carries_original_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raised ``SecretNotFound`` exposes the caller-supplied handle (not the prefixed key)."""
    monkeypatch.delenv("EMONK_MISSING", raising=False)
    resolver = EnvSecretResolver(prefix="EMONK_")
    with pytest.raises(SecretNotFound) as excinfo:
        await resolver.resolve("MISSING")
    assert excinfo.value.handle == "MISSING"
