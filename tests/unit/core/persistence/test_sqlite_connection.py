"""configure_connection must survive a concurrent first switch to WAL.

``PRAGMA journal_mode=WAL`` takes an exclusive lock that does not honor
``busy_timeout``. Parallel opens of a new database file raise
``database is locked`` unless that one statement is retried.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import aiosqlite
import pytest

from monkeybot.core.persistence.sqlite import configure_connection, open_connection

_CHILD = textwrap.dedent(
    """\
    import asyncio
    import sys
    import time

    from monkeybot.core.persistence.sqlite import apply_schema, open_connection

    async def main() -> None:
        path, start = sys.argv[1], float(sys.argv[2])
        delay = start - time.time()
        if delay > 0:
            time.sleep(delay)
        conn = await open_connection("sqlite:///" + path)
        try:
            await apply_schema(conn)
            await conn.execute("CREATE TABLE IF NOT EXISTS spike(x INTEGER)")
            await conn.execute("INSERT INTO spike(x) VALUES (1)")
        finally:
            await conn.close()

    asyncio.run(main())
    """
)


class _ScriptedConnection:
    """Forwards executes to a real connection.

    The first ``failures`` ``PRAGMA journal_mode=WAL`` calls raise
    ``database is locked``. ``failures is None`` raises on every attempt.
    """

    def __init__(self, conn: aiosqlite.Connection, failures: int | None) -> None:
        self._conn = conn
        self._failures_left = failures
        self.wal_attempts = 0
        self.sql: list[str] = []

    async def execute(self, sql: str, *args: object, **kwargs: object) -> aiosqlite.Cursor:
        self.sql.append(sql)
        if "journal_mode=WAL" in sql:
            self.wal_attempts += 1
            if self._failures_left is None or self._failures_left > 0:
                if self._failures_left is not None:
                    self._failures_left -= 1
                raise sqlite3.OperationalError("database is locked")
        return await self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


async def _no_sleep(_seconds: float) -> None:
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


async def test_configure_connection_retries_locked_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("monkeybot.core.persistence.sqlite.asyncio.sleep", _no_sleep)
    db = tmp_path / "retry.db"
    conn = await open_connection(f"sqlite:///{db}")
    try:
        scripted = _ScriptedConnection(conn, failures=2)
        await configure_connection(scripted)
        assert scripted.wal_attempts == 3
        mode = await conn.execute("PRAGMA journal_mode")
        row = await mode.fetchone()
        await mode.close()
        assert row is not None
        assert str(row[0]).lower() == "wal"
        busy = await conn.execute("PRAGMA busy_timeout")
        busy_row = await busy.fetchone()
        await busy.close()
        assert busy_row is not None
        assert busy_row[0] == 5000
    finally:
        await conn.close()


async def test_configure_connection_sets_busy_timeout_before_wal(tmp_path: Path) -> None:
    db = tmp_path / "order.db"
    conn = await open_connection(f"sqlite:///{db}")
    try:
        scripted = _ScriptedConnection(conn, failures=0)
        await configure_connection(scripted)
        wal_at = next(i for i, sql in enumerate(scripted.sql) if "journal_mode" in sql)
        busy_at = next(i for i, sql in enumerate(scripted.sql) if "busy_timeout" in sql)
        assert busy_at < wal_at
    finally:
        await conn.close()


async def test_configure_connection_wal_retry_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.persistence import sqlite as sqlite_mod

    monkeypatch.setattr(sqlite_mod.asyncio, "sleep", _no_sleep)
    db = tmp_path / "bounded.db"
    conn = await open_connection(f"sqlite:///{db}")
    try:
        scripted = _ScriptedConnection(conn, failures=None)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await configure_connection(scripted)
        assert scripted.wal_attempts == len(sqlite_mod._WAL_RETRY_DELAYS_MS)
    finally:
        await conn.close()


async def test_open_connection_memory_db() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        cursor = await conn.execute("SELECT 1")
        row = await cursor.fetchone()
        await cursor.close()
        assert row == (1,)
    finally:
        await conn.close()


@pytest.mark.slow
def test_concurrent_opens_do_not_lock(tmp_path: Path) -> None:
    """Ten processes opening one new file must all finish. Five rounds."""
    script = tmp_path / "open_child.py"
    script.write_text(_CHILD)
    env = os.environ.copy()
    root = str(_repo_root())
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    n = 10
    for rnd in range(5):
        db = tmp_path / f"round-{rnd}.db"
        start = time.time() + 20
        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(db), str(start)],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(n)
        ]
        for proc in procs:
            try:
                _out, err = proc.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, err = proc.communicate()
                raise AssertionError(f"round {rnd} child hung: {err.decode()[-500:]}") from None
            text = err.decode()
            assert proc.returncode == 0, text[-1000:]
            assert "database is locked" not in text
