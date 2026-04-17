"""Testcontainers-gated integration tests for :class:`MongoJobStorage`.

Skipped cleanly when ``motor`` or ``testcontainers`` are missing, or
when Docker is not reachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("motor")
pytest.importorskip("testcontainers.mongodb")

from src.core.harness.extensions._mongo_client import close_all  # noqa: E402
from src.core.harness.extensions.job_storage import MongoJobStorage  # noqa: E402

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
async def storage(mongo_container: str) -> AsyncIterator[MongoJobStorage]:
    env_name = f"MONGO_URI_{uuid.uuid4().hex[:8].upper()}"
    database = f"emonk_test_{uuid.uuid4().hex[:8]}"
    os.environ[env_name] = mongo_container
    backend = MongoJobStorage(uri_env=env_name, database=database)
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_job_c_01_single_winner_under_contention(
    storage: MongoJobStorage,
) -> None:
    """JOB-C-01: Mongo's document-atomic find_one_and_update picks one winner."""
    await storage.save_jobs([{"job_id": "race", "payload": {}}])
    results = await asyncio.gather(*[storage.claim_job("race") for _ in range(16)])
    assert results.count(True) == 1
    assert results.count(False) == 15


async def test_job_c_02_claim_fails_while_leased(
    storage: MongoJobStorage,
) -> None:
    """JOB-C-02: a still-leased job cannot be claimed by a second caller."""
    await storage.save_jobs([{"job_id": "leased", "payload": {}}])
    assert await storage.claim_job("leased", lease_duration_seconds=60)
    assert not await storage.claim_job("leased", lease_duration_seconds=60)


async def test_job_c_02_lease_expiry_reclaim(
    storage: MongoJobStorage,
) -> None:
    """JOB-C-02 extension: expired lease allows a fresh claim."""
    await storage.save_jobs([{"job_id": "expiring", "payload": {}}])
    assert await storage.claim_job("expiring", lease_duration_seconds=0)
    await asyncio.sleep(0.05)
    assert await storage.claim_job("expiring", lease_duration_seconds=60)


async def test_job_c_03_release_allows_reclaim(
    storage: MongoJobStorage,
) -> None:
    """JOB-C-03: release_job clears the lease."""
    await storage.save_jobs([{"job_id": "reclaim", "payload": {}}])
    assert await storage.claim_job("reclaim")
    await storage.release_job("reclaim")
    assert await storage.claim_job("reclaim")


async def test_job_c_04_save_then_load_round_trip(
    storage: MongoJobStorage,
) -> None:
    """JOB-C-04: save_jobs + load_jobs round-trips payloads by ``job_id``."""
    await storage.save_jobs(
        [
            {"job_id": "a", "payload": {"n": 1}},
            {"job_id": "b", "payload": {"n": 2}},
        ]
    )
    loaded = await storage.load_jobs()
    by_id = {job["job_id"]: job for job in loaded}
    assert set(by_id) == {"a", "b"}
    assert by_id["a"]["payload"] == {"n": 1}
