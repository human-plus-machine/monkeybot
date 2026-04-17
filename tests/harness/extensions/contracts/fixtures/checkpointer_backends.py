"""Factory list fed to the :mod:`test_checkpointer_contract` parametrization.

Story 1 seeds the contract suite with just the ``MockCheckpointer``. Story 2
layers in real backends — the in-memory implementation is always available,
and Postgres/Mongo/Firestore append when their optional dependencies (and
Docker for testcontainers) are present.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.harness.extensions._mocks import MockCheckpointer
from src.core.harness.extensions.checkpointers import InMemoryCheckpointer

CHECKPOINTER_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockCheckpointer()),
    ("in_memory", lambda: InMemoryCheckpointer()),
]


__all__ = ["CHECKPOINTER_FACTORIES"]
