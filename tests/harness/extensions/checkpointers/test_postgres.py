"""Testcontainers-gated integration tests for :class:`PostgresCheckpointer`.

Skipped cleanly when ``asyncpg``, ``orjson`` or ``testcontainers`` are not
installed, or when Docker is not reachable. Runs the CKPT-C-01…07 invariants
including a 100-parallel-write stress test.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("orjson")
pytest.importorskip("testcontainers.postgres")

from src.core.harness.extensions import CheckpointMissing  # noqa: E402
from src.core.harness.extensions._postgres_pool import close_all  # noqa: E402
from src.core.harness.extensions.checkpointers import PostgresCheckpointer  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module")
def postgres_container() -> AsyncIterator[str]:  # type: ignore[misc]
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # pragma: no cover - docker not reachable
        pytest.skip(f"Docker unavailable: {exc}")
        return  # type: ignore[unreachable]
    try:
        dsn = container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
        yield dsn
    finally:
        container.stop()


@pytest.fixture
async def ckpt(postgres_container: str) -> AsyncIterator[PostgresCheckpointer]:
    env_name = f"CKPT_DSN_{uuid.uuid4().hex[:8].upper()}"
    schema = f"ckpt_test_{uuid.uuid4().hex[:8]}"
    os.environ[env_name] = postgres_container
    backend = PostgresCheckpointer(dsn_env=env_name, schema_name=schema)
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_ckpt_c_01_monotonic_ids(ckpt: PostgresCheckpointer) -> None:
    a = await ckpt.write("s", {"v": 1})
    b = await ckpt.write("s", {"v": 2})
    assert a.checkpoint_id < b.checkpoint_id


async def test_ckpt_c_02_read_latest(ckpt: PostgresCheckpointer) -> None:
    await ckpt.write("s", {"v": 1})
    await ckpt.write("s", {"v": 2})
    await ckpt.write("s", {"v": 3})
    assert await ckpt.read("s") == {"v": 3}


async def test_ckpt_c_03_read_by_id(ckpt: PostgresCheckpointer) -> None:
    ref = await ckpt.write("s", {"payload": "value"})
    assert await ckpt.read("s", ref.checkpoint_id) == {"payload": "value"}


async def test_ckpt_c_04_list_newest_first(ckpt: PostgresCheckpointer) -> None:
    refs = [await ckpt.write("s", {"i": i}) for i in range(5)]
    listed = await ckpt.list("s", limit=3)
    assert [r.checkpoint_id for r in listed] == [
        refs[-1].checkpoint_id,
        refs[-2].checkpoint_id,
        refs[-3].checkpoint_id,
    ]


async def test_ckpt_c_05_delete_session(ckpt: PostgresCheckpointer) -> None:
    ref = await ckpt.write("s", {"v": 1})
    await ckpt.delete_session("s")
    assert await ckpt.read("s") is None
    with pytest.raises(CheckpointMissing):
        await ckpt.read("s", ref.checkpoint_id)


async def test_ckpt_c_06_concurrent_writes_distinct(ckpt: PostgresCheckpointer) -> None:
    refs = await asyncio.gather(*[ckpt.write("s", {"i": i}) for i in range(100)])
    assert len({ref.checkpoint_id for ref in refs}) == 100


async def test_ckpt_c_07_large_payload_round_trip(ckpt: PostgresCheckpointer) -> None:
    payload = {"blob": "a" * 1_000_000}
    ref = await ckpt.write("s", payload)
    assert await ckpt.read("s", ref.checkpoint_id) == payload
