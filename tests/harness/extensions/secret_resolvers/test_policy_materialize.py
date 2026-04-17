"""Tests for the Story 6 ``Policy.materialize`` branch.

``Policy.materialize(None)`` must preserve the legacy env-only behaviour
(backward-compatible with pre-Story-6 call sites), while
``Policy.materialize(resolver)`` must dereference every configured handle
through the resolver and populate the returned env map only.
"""

from __future__ import annotations

import pytest

from src.core.harness.extensions._mocks import MockSecretResolver
from src.core.harness.sandbox.policy import Policy
from src.core.harness.specs import PolicySpec

pytestmark = pytest.mark.asyncio


def _policy(secret_handles: dict[str, str]) -> Policy:
    spec = PolicySpec(
        fs_allow=[],
        fs_deny=[],
        net_allow=[],
        net_deny=[],
        env_allow=[],
        secret_handles=secret_handles,
    )
    return Policy.from_spec(spec, timeout_seconds=30)


async def test_materialize_none_uses_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a resolver, handles are resolved against ``os.environ``."""
    monkeypatch.setenv("DB_SECRET", "the-secret")
    policy = _policy({"DB_PASS": "DB_SECRET"})

    env = await policy.materialize(None)

    assert env == {"DB_PASS": "the-secret"}


async def test_materialize_none_skips_missing_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env vars are silently skipped (legacy behaviour)."""
    monkeypatch.delenv("__ABSENT_HANDLE__", raising=False)
    policy = _policy({"DB_PASS": "__ABSENT_HANDLE__"})

    env = await policy.materialize(None)

    assert env == {}


async def test_materialize_with_resolver_uses_resolver() -> None:
    """A supplied resolver is called for each handle; output lands in the env map."""
    resolver = MockSecretResolver({"DB_SECRET": "resolved-value", "API_TOKEN": "token-1"})
    policy = _policy({"DB_PASS": "DB_SECRET", "API_KEY": "API_TOKEN"})

    env = await policy.materialize(resolver)

    assert env == {"DB_PASS": "resolved-value", "API_KEY": "token-1"}


async def test_materialize_with_resolver_ignores_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a resolver is supplied, ``os.environ`` is NOT consulted."""
    monkeypatch.setenv("DB_SECRET", "from-env-should-not-win")
    resolver = MockSecretResolver({"DB_SECRET": "resolver-wins"})
    policy = _policy({"DB_PASS": "DB_SECRET"})

    env = await policy.materialize(resolver)

    assert env == {"DB_PASS": "resolver-wins"}


async def test_materialize_empty_handles_returns_empty_dict() -> None:
    """A policy with no secret handles always returns an empty mapping."""
    policy = _policy({})
    assert await policy.materialize(None) == {}
    assert await policy.materialize(MockSecretResolver({})) == {}
