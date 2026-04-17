"""Testcontainers-gated integration tests for :class:`MongoCheckpointer`.

Skipped cleanly when ``motor``, ``orjson`` or ``testcontainers`` are missing,
or when Docker is unreachable. Exercises CKPT-C-01…07 against a disposable
MongoDB container.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("motor")
pytest.importorskip("orjson")
pytest.importorskip("testcontainers.mongodb")

from src.core.harness.extensions import CheckpointMissing  # noqa: E402
from src.core.harness.extensions._mongo_client import close_all  # noqa: E402
from src.core.harness.extensions.checkpointers import MongoCheckpointer  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module")
def mongo_container() -> AsyncIterator[str]:  # type: ignore[misc]
    from testcontainers.mongodb import MongoDbContainer

    try:
        container = MongoDbContainer("mongo:7")
        container.start()
    except Exception as exc:  # pragma: no cover - docker not reachable
        pytest.skip(f"Docker unavailable: {exc}")
        return  # type: ignore[unreachable]
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
async def ckpt(mongo_container: str) -> AsyncIterator[MongoCheckpointer]:
    env_name = f"MONGO_URI_{uuid.uuid4().hex[:8].upper()}"
    database = f"emonk_test_{uuid.uuid4().hex[:8]}"
    os.environ[env_name] = mongo_container
    backend = MongoCheckpointer(uri_env=env_name, database=database)
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_ckpt_c_01_monotonic_ids(ckpt: MongoCheckpointer) -> None:
    a = await ckpt.write("s", {"v": 1})
    b = await ckpt.write("s", {"v": 2})
    assert a.checkpoint_id < b.checkpoint_id


async def test_ckpt_c_02_read_latest(ckpt: MongoCheckpointer) -> None:
    await ckpt.write("s", {"v": 1})
    await ckpt.write("s", {"v": 2})
    await ckpt.write("s", {"v": 3})
    assert await ckpt.read("s") == {"v": 3}


async def test_ckpt_c_03_read_by_id(ckpt: MongoCheckpointer) -> None:
    ref = await ckpt.write("s", {"payload": "value"})
    assert await ckpt.read("s", ref.checkpoint_id) == {"payload": "value"}


async def test_ckpt_c_04_list_newest_first(ckpt: MongoCheckpointer) -> None:
    refs = [await ckpt.write("s", {"i": i}) for i in range(5)]
    listed = await ckpt.list("s", limit=3)
    assert [r.checkpoint_id for r in listed] == [
        refs[-1].checkpoint_id,
        refs[-2].checkpoint_id,
        refs[-3].checkpoint_id,
    ]


async def test_ckpt_c_05_delete_session(ckpt: MongoCheckpointer) -> None:
    ref = await ckpt.write("s", {"v": 1})
    await ckpt.delete_session("s")
    assert await ckpt.read("s") is None
    with pytest.raises(CheckpointMissing):
        await ckpt.read("s", ref.checkpoint_id)


async def test_ckpt_c_06_concurrent_writes_distinct(ckpt: MongoCheckpointer) -> None:
    refs = await asyncio.gather(*[ckpt.write("s", {"i": i}) for i in range(100)])
    assert len({ref.checkpoint_id for ref in refs}) == 100


async def test_ckpt_c_07_large_payload_round_trip(ckpt: MongoCheckpointer) -> None:
    payload = {"blob": "a" * 800_000}
    ref = await ckpt.write("s", payload)
    assert await ckpt.read("s", ref.checkpoint_id) == payload
