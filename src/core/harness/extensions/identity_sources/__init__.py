"""Builtin :class:`IdentitySource` backends + registry wiring (Story 5).

Importing this package registers all six named backends plus the
``callable`` escape hatch against :class:`IdentitySource.registry`.
Optional SDKs (``aioboto3``, ``motor``, ``asyncpg``, ``google.cloud.*``)
are imported lazily inside the individual modules so just importing this
package is free of those dependencies.
"""

from __future__ import annotations

import contextlib

from ..base import IdentitySource
from ..errors import BackendConfigError
from .callable import CallableIdentitySource
from .gcs import GCSIdentitySource
from .local_fs import LocalFSIdentitySource
from .mongo import MongoIdentitySource
from .postgres import PostgresIdentitySource
from .s3 import S3IdentitySource


def _register_once(name: str, factory: type[IdentitySource]) -> None:
    """Register ``factory`` under ``name`` if not already registered as a builtin."""
    existing = IdentitySource.registry.entry(name)
    if existing is not None and existing.source == "builtin":
        return
    with contextlib.suppress(BackendConfigError):  # pragma: no cover - defensive
        IdentitySource.registry.register(name, factory, source="builtin")


_register_once("local_fs", LocalFSIdentitySource)
_register_once("s3", S3IdentitySource)
_register_once("gcs", GCSIdentitySource)
_register_once("postgres", PostgresIdentitySource)
_register_once("mongo", MongoIdentitySource)
_register_once("callable", CallableIdentitySource)


__all__ = [
    "CallableIdentitySource",
    "GCSIdentitySource",
    "LocalFSIdentitySource",
    "MongoIdentitySource",
    "PostgresIdentitySource",
    "S3IdentitySource",
]
