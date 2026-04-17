"""Builtin :class:`SecretResolver` backends + registry wiring (Story 6).

Importing this package registers the four shipped backends (``env``,
``aws``, ``gcp``, ``composite``) against :class:`SecretResolver.registry`.
Optional SDKs (``aioboto3``, ``google-cloud-secret-manager``) are imported
lazily inside the individual modules so simply importing this package is
free of those dependencies.
"""

from __future__ import annotations

import contextlib

from ..base import SecretResolver
from ..errors import BackendConfigError
from ._resolve_tracer import TracingResolver
from .aws import AWSSecretsManagerResolver
from .composite import CompositeSecretResolver
from .env import EnvSecretResolver
from .gcp import GCPSecretManagerResolver


def _register_once(name: str, factory: type[SecretResolver]) -> None:
    """Register ``factory`` under ``name`` if not already registered as a builtin."""
    existing = SecretResolver.registry.entry(name)
    if existing is not None and existing.source == "builtin":
        return
    with contextlib.suppress(BackendConfigError):  # pragma: no cover - defensive
        SecretResolver.registry.register(name, factory, source="builtin")


_register_once("env", EnvSecretResolver)
_register_once("aws", AWSSecretsManagerResolver)
_register_once("gcp", GCPSecretManagerResolver)
_register_once("composite", CompositeSecretResolver)


__all__ = [
    "AWSSecretsManagerResolver",
    "CompositeSecretResolver",
    "EnvSecretResolver",
    "GCPSecretManagerResolver",
    "TracingResolver",
]
