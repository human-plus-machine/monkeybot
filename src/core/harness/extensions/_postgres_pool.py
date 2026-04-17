"""Shared async Postgres pool helper.

One ``asyncpg.Pool`` is memoised per ``(dsn_env, schema_name)`` pair so
backends that target the same database share a pool (see 1C §2.2). DDL is
executed idempotently on the first ``get_pool`` call for a given key.

Stories 3, 4 and 5 also import from this module — do not couple it to the
checkpointer surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from .errors import BackendConfigError

_POOLS: dict[tuple[str, str], Any] = {}
_POOL_LOCK = asyncio.Lock()


async def get_pool(
    *,
    dsn_env: str,
    schema_name: str,
    ddl_path: Path,
    min_size: int = 1,
    max_size: int = 10,
    command_timeout: float = 5.0,
) -> Any:
    """Return (creating once) the shared pool for ``(dsn_env, schema_name)``.

    Args:
        dsn_env: Environment variable name holding the Postgres DSN.
        schema_name: Schema to create and use; substituted into ``ddl_path``.
        ddl_path: Path to an idempotent SQL file with ``__SCHEMA__`` tokens.
        min_size: Minimum pool connections.
        max_size: Maximum pool connections.
        command_timeout: Default asyncpg command timeout in seconds.

    Raises:
        BackendConfigError: ``dsn_env`` is unset or the DSN fails to connect.
    """
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - optional dep
        raise BackendConfigError(
            "PostgresCheckpointer requires emonk[checkpointer-postgres] (asyncpg)"
        ) from exc

    key = (dsn_env, schema_name)
    async with _POOL_LOCK:
        cached = _POOLS.get(key)
        if cached is not None:
            return cached
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise BackendConfigError(
                f"Postgres backend: environment variable {dsn_env!r} is not set"
            )
        try:
            pool = await asyncpg.create_pool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                command_timeout=command_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - asyncpg error hierarchy is broad
            raise BackendConfigError(
                f"Postgres backend: failed to connect using {dsn_env!r}: {exc}"
            ) from exc

        ddl = ddl_path.read_text().replace("__SCHEMA__", schema_name)
        async with pool.acquire() as conn:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
            await conn.execute(ddl)
        _POOLS[key] = pool
        return pool


async def close_all() -> None:
    """Close every cached pool. Intended for test teardown."""
    async with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive teardown
            await pool.close()


__all__ = ["close_all", "get_pool"]
