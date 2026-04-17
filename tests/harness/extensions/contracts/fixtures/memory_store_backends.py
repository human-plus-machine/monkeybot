"""Factory list fed to the :mod:`test_memory_store_contract` parametrization.

Story 1 seeded the contract suite with just the ``MockMemoryStore``. Story 3
adds the always-available ``InMemoryMemoryStore`` so MEM-C-01…07 runs
against both in-process reference backends. Cloud backends (Postgres,
Mongo, S3, Firestore, GCS) are tested in their dedicated test modules —
those need module-scoped containers or fake-SDK fixtures that cannot be
expressed as a zero-argument factory.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.harness.extensions._mocks import MockMemoryStore
from src.core.harness.extensions.memory_stores import InMemoryMemoryStore

MEMORY_STORE_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockMemoryStore()),
    ("in_memory", lambda: InMemoryMemoryStore()),
]


__all__ = ["MEMORY_STORE_FACTORIES"]
