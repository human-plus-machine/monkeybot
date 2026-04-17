"""Registry wiring tests for Story 6 ``SecretResolver`` builtins."""

from __future__ import annotations

from src.core.harness.extensions import SecretResolver
from src.core.harness.extensions import secret_resolvers as _resolvers  # noqa: F401
from src.core.harness.extensions.secret_resolvers import (
    AWSSecretsManagerResolver,
    CompositeSecretResolver,
    EnvSecretResolver,
    GCPSecretManagerResolver,
)


def test_four_builtins_are_registered() -> None:
    """Importing the package registers ``env``, ``aws``, ``gcp``, ``composite``."""
    names = {entry.name for entry in SecretResolver.registry.entries() if entry.source == "builtin"}
    for required in ("env", "aws", "gcp", "composite"):
        assert required in names, f"missing builtin {required!r} in {sorted(names)}"


def test_registry_resolve_env_backend() -> None:
    """``resolve({"backend": "env", ...})`` returns a fresh :class:`EnvSecretResolver`."""
    resolver = SecretResolver.registry.resolve({"backend": "env", "prefix": "EMONK_"})
    assert isinstance(resolver, EnvSecretResolver)
    assert resolver.prefix == "EMONK_"


def test_registry_resolve_composite_backend() -> None:
    """``composite`` resolves into a :class:`CompositeSecretResolver` with an empty chain."""
    resolver = SecretResolver.registry.resolve({"backend": "composite", "chain": []})
    assert isinstance(resolver, CompositeSecretResolver)


def test_factories_point_at_shipped_classes() -> None:
    """The four registered entries wire the classes re-exported from the package."""
    entries = {entry.name: entry for entry in SecretResolver.registry.entries()}
    assert entries["env"].factory_qualname.endswith("EnvSecretResolver")
    assert entries["aws"].factory_qualname.endswith("AWSSecretsManagerResolver")
    assert entries["gcp"].factory_qualname.endswith("GCPSecretManagerResolver")
    assert entries["composite"].factory_qualname.endswith("CompositeSecretResolver")

    assert EnvSecretResolver is not None
    assert AWSSecretsManagerResolver is not None
    assert GCPSecretManagerResolver is not None
    assert CompositeSecretResolver is not None
