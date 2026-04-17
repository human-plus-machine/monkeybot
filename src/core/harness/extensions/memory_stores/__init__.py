"""Builtin :class:`MemoryStore` backends + registry wiring.

Importing this package registers all six shipped backends (``in_memory``,
``firestore``, ``gcs``, ``s3``, ``postgres``, ``mongo``) against
``MemoryStore.registry`` with ``source="builtin"``. Optional SDKs
(``aioboto3``, ``asyncpg``, ``motor``, ``google.cloud.*``) are not imported
here — the concrete backends load them lazily on first use.
"""

from __future__ import annotations

import contextlib

from ..base import MemoryStore
from ..errors import BackendConfigError
from .firestore import FirestoreMemoryStore
from .gcs import GCSMemoryStore
from .in_memory import InMemoryMemoryStore
from .mongo import MongoMemoryStore
from .postgres import PostgresMemoryStore
from .s3 import S3MemoryStore


def _register_once(name: str, factory: type[MemoryStore]) -> None:
    """Register ``factory`` under ``name`` if not already registered as a builtin."""
    existing = MemoryStore.registry.entry(name)
    if existing is not None and existing.source == "builtin":
        return
    with contextlib.suppress(BackendConfigError):  # pragma: no cover - defensive
        MemoryStore.registry.register(name, factory, source="builtin")


_register_once("in_memory", InMemoryMemoryStore)
_register_once("firestore", FirestoreMemoryStore)
_register_once("gcs", GCSMemoryStore)
_register_once("s3", S3MemoryStore)
_register_once("postgres", PostgresMemoryStore)
_register_once("mongo", MongoMemoryStore)


__all__ = [
    "FirestoreMemoryStore",
    "GCSMemoryStore",
    "InMemoryMemoryStore",
    "MongoMemoryStore",
    "PostgresMemoryStore",
    "S3MemoryStore",
]
