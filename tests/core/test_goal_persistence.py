"""Cross-backend contracts for native goal columns and tick completion."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest

from monkeybot.core.goals.service import DurableGoalService
from monkeybot.core.persistence.scheduled_loops import (
    KIND_GOAL,
    KIND_LOOP,
    OPEN_GOAL_STATUSES,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    planned_create,
    resolve_complete_tick,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

_POSTGRES_ENV = "MONKEYBOT_TEST_POSTGRES_URL"
_POSTGRES_ENV_ALIAS = "TEST_POSTGRES_URL"


def test_planned_create_goal_is_unbounded_and_deferred() -> None:
    values = planned_create(
        ScheduledLoopCreate(
            prompt="Ship it",
            interval_ms=300_000,
            session_id="sess-1",
            loop_id="goal-1",
            kind=KIND_GOAL,
            objective="Ship it",
        ),
        now_ms=1_000,
    )
    assert values["kind"] == KIND_GOAL
    assert values["objective"] == "Ship it"
    assert values["max_ticks"] is None
    assert values["max_runtime_ms"] is None
    assert values["next_tick_at_ms"] == 1_000 + 300_000
    loop_values = planned_create(
        ScheduledLoopCreate(
            prompt="poll",
            interval_ms=5_000,
            max_ticks=3,
            kind=KIND_LOOP,
        ),
        now_ms=1_000,
    )
    assert loop_values["kind"] == KIND_LOOP
    assert loop_values["objective"] is None
    assert loop_values["next_tick_at_ms"] == 1_000


def test_resolve_complete_tick_loop_error_still_fails() -> None:
    row = ScheduledLoopRow(
        loop_id="loop-1",
        session_id="sess-1",
        status="active",
        prompt="poll",
        interval_ms=5_000,
        max_ticks=3,
        max_runtime_ms=None,
        skip_if_busy=True,
        tick_index=0,
        next_tick_at_ms=1,
        started_at_ms=0,
        last_tick_at_ms=None,
        last_error=None,
        stop_reason=None,
        tick_in_flight=True,
        kind=KIND_LOOP,
    )
    tick_index, status, stop_reason, _, last_error = resolve_complete_tick(
        row, error="boom", now_ms=1_000
    )
    assert tick_index == 1
    assert status == "failed"
    assert stop_reason == "tick_error"
    assert last_error == "boom"


@pytest.mark.asyncio
async def test_sqlite_list_kind_and_find_open_ignore_loops(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'goals.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    await store.create(
        ScheduledLoopCreate(
            prompt="poll inbox",
            interval_ms=5_000,
            session_id="sess-1",
            loop_id="loop-1",
            max_ticks=3,
            kind=KIND_LOOP,
        )
    )
    goal = await store.create(
        ScheduledLoopCreate(
            prompt="Ship it",
            interval_ms=300_000,
            session_id="sess-1",
            loop_id="goal-1",
            kind=KIND_GOAL,
            objective="Ship it",
        )
    )
    goals = await store.list_kind(KIND_GOAL)
    assert [row.loop_id for row in goals] == ["goal-1"]
    opened = await store.find_open(
        session_id="sess-1",
        kind=KIND_GOAL,
        statuses=OPEN_GOAL_STATUSES,
    )
    assert opened is not None
    assert opened.loop_id == goal.loop_id
    await backend.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _with_database(url: str, name: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{name}"))


async def _wait_for_postgres(url: str, *, timeout_s: float = 40) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(url, timeout=2)
            await conn.close()
            return
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.4)
    raise RuntimeError(f"postgres did not become ready: {last}")


def _postgres_bin_dir() -> Path | None:
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    for candidate in (
        Path("/opt/homebrew/opt/postgresql@16/bin"),
        Path("/opt/homebrew/opt/postgresql@17/bin"),
        Path("/usr/lib/postgresql/16/bin"),
        Path("/usr/lib/postgresql/15/bin"),
    ):
        if (candidate / "pg_ctl").is_file():
            return candidate
    return None


@dataclass
class _PgServer:
    admin_url: str
    stop: Callable[[], None]


def _start_pg_ctl_postgres() -> _PgServer | None:
    bin_dir = _postgres_bin_dir()
    if bin_dir is None:
        return None
    port = _free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="mb-pg-"))
    init = subprocess.run(
        [
            str(bin_dir / "initdb"),
            "-D",
            str(data_dir),
            "-U",
            "test",
            "--auth=trust",
        ],
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        shutil.rmtree(data_dir, ignore_errors=True)
        return None
    started = subprocess.run(
        [
            str(bin_dir / "pg_ctl"),
            "-D",
            str(data_dir),
            "-l",
            str(data_dir / "pg.log"),
            "-o",
            f"-p {port} -c listen_addresses=127.0.0.1",
            "start",
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        shutil.rmtree(data_dir, ignore_errors=True)
        return None

    def stop() -> None:
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "stop"],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(data_dir, ignore_errors=True)

    return _PgServer(f"postgresql://test@127.0.0.1:{port}/postgres", stop)


def _start_docker_postgres() -> _PgServer | None:
    if (
        subprocess.call(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        != 0
    ):
        return None
    port = _free_port()
    name = f"monkeybot-goal-pg-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=test",
            "-e",
            "POSTGRES_PASSWORD=test",
            "-e",
            "POSTGRES_DB=postgres",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:16-alpine",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    def stop() -> None:
        subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            capture_output=True,
            text=True,
        )

    return _PgServer(f"postgresql://test:test@127.0.0.1:{port}/postgres", stop)


def _start_ephemeral_postgres() -> _PgServer:
    server = _start_pg_ctl_postgres() or _start_docker_postgres()
    if server is None:
        pytest.skip(
            "real Postgres required: set MONKEYBOT_TEST_POSTGRES_URL, or install "
            "postgresql (pg_ctl), or start Docker"
        )
    return server


@asynccontextmanager
async def _postgres_backend() -> AsyncIterator[Any]:
    asyncpg = pytest.importorskip("asyncpg")
    from monkeybot.core.persistence.postgres import PostgresStorageBackend

    env_url = os.environ.get(_POSTGRES_ENV) or os.environ.get(_POSTGRES_ENV_ALIAS)
    server = None if env_url else _start_ephemeral_postgres()
    admin_url = env_url or (server.admin_url if server is not None else "")
    db_name = f"mb_goal_{uuid.uuid4().hex[:10]}"
    backend: PostgresStorageBackend | None = None
    admin = None
    try:
        await _wait_for_postgres(admin_url)
        admin = await asyncpg.connect(admin_url)
        await admin.execute(f'CREATE DATABASE "{db_name}"')
        await admin.close()
        admin = None
        backend = PostgresStorageBackend(_with_database(admin_url, db_name))
        await backend.open()
        yield backend
    finally:
        if backend is not None:
            await backend.close()
        if admin is not None:
            await admin.close()
            admin = None
        if admin_url:
            try:
                admin = await asyncpg.connect(admin_url)
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    db_name,
                )
                await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            except Exception:
                pass
            finally:
                if admin is not None:
                    await admin.close()
        if server is not None:
            server.stop()


@pytest.mark.asyncio
async def test_postgres_goal_queries_run() -> None:
    async with _postgres_backend() as backend:
        store = backend.scheduled_loops()
        await store.create(
            ScheduledLoopCreate(
                prompt="poll inbox",
                interval_ms=5_000,
                session_id="sess-1",
                loop_id="loop-1",
                max_ticks=3,
                kind=KIND_LOOP,
            )
        )
        created = await store.create(
            ScheduledLoopCreate(
                prompt="Ship it",
                interval_ms=1,
                session_id="sess-1",
                loop_id="goal-pg-1",
                kind=KIND_GOAL,
                objective="Ship it",
            )
        )
        assert created.kind == KIND_GOAL
        assert created.objective == "Ship it"
        assert created.max_ticks is None

        goals = await store.list_kind(KIND_GOAL)
        assert [row.loop_id for row in goals] == ["goal-pg-1"]
        opened = await store.find_open(
            session_id="sess-1",
            kind=KIND_GOAL,
            statuses=OPEN_GOAL_STATUSES,
        )
        assert opened is not None
        assert opened.loop_id == "goal-pg-1"

        await asyncio.sleep(0.05)
        claimed = await store.claim_tick("goal-pg-1", "worker-1")
        assert claimed is not None
        completed = await store.complete_tick("goal-pg-1", worker_id="worker-1", error="timeout")
        assert completed is not None
        assert completed.status == "active"
        assert completed.last_error == "timeout"

        service = DurableGoalService(store)
        paused = await service.pause("goal-pg-1")
        assert paused.status == "paused"
        stopped = await service.stop("goal-pg-1")
        assert stopped.status == "completed"
        assert await service.open_for_session("sess-1") is None
