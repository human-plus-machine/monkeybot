"""Builtin :class:`Checkpointer` backends + registry wiring.

Importing this package registers all four shipped backends (``in_memory``,
``firestore``, ``postgres``, ``mongo``) against ``Checkpointer.registry``
with ``source="builtin"``.
"""

from __future__ import annotations

import contextlib

from ..base import Checkpointer
from ..errors import BackendConfigError
from .firestore import FirestoreCheckpointer
from .in_memory import InMemoryCheckpointer
from .mongo import MongoCheckpointer
from .postgres import PostgresCheckpointer


def _register_once(name: str, factory: type[Checkpointer]) -> None:
    """Register ``factory`` under ``name`` if not already registered as a builtin."""
    existing = Checkpointer.registry.entry(name)
    if existing is not None and existing.source == "builtin":
        return
    with contextlib.suppress(BackendConfigError):  # pragma: no cover - defensive
        Checkpointer.registry.register(name, factory, source="builtin")


_register_once("in_memory", InMemoryCheckpointer)
_register_once("firestore", FirestoreCheckpointer)
_register_once("postgres", PostgresCheckpointer)
_register_once("mongo", MongoCheckpointer)


__all__ = [
    "FirestoreCheckpointer",
    "InMemoryCheckpointer",
    "MongoCheckpointer",
    "PostgresCheckpointer",
]
