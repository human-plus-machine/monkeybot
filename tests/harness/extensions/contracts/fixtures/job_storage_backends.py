"""Factory list fed to the :mod:`test_job_storage_contract` parametrization.

Story 1 seeded the contract suite with just the ``MockJobStorage``.
Story 4 layers in the always-available ``JSONFileJobStorage`` (via a
temp-file path) so JOB-C-01 … JOB-C-04 runs against both in-process
reference backends. Cloud backends (Postgres, Mongo, Firestore) are
tested in their dedicated test modules — those require module-scoped
containers or fake-SDK fixtures that cannot be expressed as a
zero-argument factory.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.core.harness.extensions._mocks import MockJobStorage
from src.core.harness.extensions.job_storage import JSONFileJobStorage


def _json_file_factory() -> JSONFileJobStorage:
    """Return a ``JSONFileJobStorage`` anchored at a fresh temp path."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="emonk_jobs_"))
    return JSONFileJobStorage(tmp_dir / f"jobs_{uuid.uuid4().hex}.json")


JOB_STORAGE_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockJobStorage()),
    ("json", _json_file_factory),
]


__all__ = ["JOB_STORAGE_FACTORIES"]
