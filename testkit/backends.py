"""Open a sqlite, Postgres, or Firestore storage backend for contract tests.

Sqlite always opens. Postgres and Firestore skip unless
``MONKEYBOT_TEST_POSTGRES_URL`` or ``FIRESTORE_EMULATOR_HOST`` is set.
The skip text lives here so every caller uses the same message.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

POSTGRES_SKIP = "MONKEYBOT_TEST_POSTGRES_URL not set"
FIRESTORE_SKIP = "FIRESTORE_EMULATOR_HOST not set"

_POSTGRES_ENV = "MONKEYBOT_TEST_POSTGRES_URL"
_FIRESTORE_ENV = "FIRESTORE_EMULATOR_HOST"


async def open_backend(
    kind: str,
    *,
    agent_scope: str = "",
    agent_root: Path | None = None,
    db_url: str | None = None,
    prefix: str = "",
) -> Any:
    """Open ``kind`` (``sqlite``, ``postgres``, or ``firestore``) and return it.

    The caller closes the backend. Sqlite requires ``db_url`` so two scopes can
    share one file; ``sqlite:///:memory:`` is a distinct database per connection.
    """
    if kind == "sqlite":
        if not db_url:
            raise ValueError("sqlite open_backend requires db_url")
        from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

        backend: Any = SQLiteStorageBackend(db_url, agent_scope=agent_scope, agent_root=agent_root)
    elif kind == "postgres":
        url = os.environ.get(_POSTGRES_ENV)
        if not url:
            pytest.skip(POSTGRES_SKIP)
        pytest.importorskip("asyncpg")
        from monkeybot.core.persistence.postgres import PostgresStorageBackend

        backend = PostgresStorageBackend(url, agent_scope=agent_scope)
    elif kind == "firestore":
        if not os.environ.get(_FIRESTORE_ENV):
            pytest.skip(FIRESTORE_SKIP)
        pytest.importorskip("google.cloud.firestore")
        from monkeybot.core.persistence.backends import FirestoreConfig
        from monkeybot.core.persistence.firestore import FirestoreStorageBackend

        backend = FirestoreStorageBackend(
            FirestoreConfig(
                project="demo-monkeybot",
                database="(default)",
                prefix=prefix,
            ),
            agent_scope=agent_scope,
        )
    else:
        raise ValueError(f"unknown backend kind: {kind}")
    await backend.open()
    return backend
