"""Builtin :class:`JobStorage` backends + registry wiring.

Importing this package registers all four shipped backends
(``json``, ``firestore``, ``postgres``, ``mongo``) against
``JobStorage.registry`` with ``source="builtin"``. The legacy name
``json_file`` is also registered as an alias for the JSON-file backend so
Phase 4 consumers that stored that literal keep working; the canonical
name going forward is ``json`` (matches
:class:`JobStorageJSONSpec.backend` literal).

Optional SDKs (``filelock``, ``asyncpg``, ``motor``,
``google.cloud.firestore``) are loaded lazily by the concrete backends.
"""

from __future__ import annotations

import contextlib

from ..base import JobStorage
from ..errors import BackendConfigError
from .firestore import FirestoreJobStorage
from .json_file import JSONFileJobStorage
from .mongo import MongoJobStorage
from .postgres import PostgresJobStorage


def _register_once(name: str, factory: type[JobStorage]) -> None:
    """Register ``factory`` under ``name`` if not already a builtin."""
    existing = JobStorage.registry.entry(name)
    if existing is not None and existing.source == "builtin":
        return
    with contextlib.suppress(BackendConfigError):  # pragma: no cover - defensive
        JobStorage.registry.register(name, factory, source="builtin")


_register_once("json", JSONFileJobStorage)
_register_once("json_file", JSONFileJobStorage)
_register_once("firestore", FirestoreJobStorage)
_register_once("postgres", PostgresJobStorage)
_register_once("mongo", MongoJobStorage)


__all__ = [
    "FirestoreJobStorage",
    "JSONFileJobStorage",
    "MongoJobStorage",
    "PostgresJobStorage",
]
