"""Testcontainers-gated integration tests for :class:`PostgresJobStorage`.

Skipped cleanly when ``asyncpg``, ``orjson``, or ``testcontainers`` are
missing, or when Docker is not reachable. Verifies JOB-C-01 under
``FOR UPDATE SKIP LOCKED`` and the 1C §2 perf budget
(``claim_job`` p99 ≤ 50 ms) — the budget check is marked
``xfail(strict=False)`` so a slow CI runner never blocks the suite.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("orjson")
pytest.importorskip("testcontainers.postgres")

from src.core.harness.extensions._postgres_pool import close_all  # noqa: E402
from src.core.harness.extensions.job_storage import PostgresJobStorage  # noqa: E402

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
        dsn = container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
        yield dsn
    finally:
        container.stop()


@pytest.fixture
async def storage(postgres_container: str) -> AsyncIterator[PostgresJobStorage]:
    env_name = f"SCHED_DSN_{uuid.uuid4().hex[:8].upper()}"
    schema = f"jobs_test_{uuid.uuid4().hex[:8]}"
    os.environ[env_name] = postgres_container
    backend = PostgresJobStorage(dsn_env=env_name, schema_name=schema)
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_job_c_01_single_winner_under_contention(
    storage: PostgresJobStorage,
) -> None:
    """JOB-C-01: FOR UPDATE SKIP LOCKED picks exactly one winner."""
    await storage.save_jobs([{"job_id": "race", "payload": {}}])
    results = await asyncio.gather(*[storage.claim_job("race") for _ in range(16)])
    assert results.count(True) == 1
    assert results.count(False) == 15


async def test_job_c_02_claim_fails_while_leased(
    storage: PostgresJobStorage,
) -> None:
    """JOB-C-02: a second caller cannot re-claim a still-leased job."""
    await storage.save_jobs([{"job_id": "leased", "payload": {}}])
    assert await storage.claim_job("leased", lease_duration_seconds=60)
    assert not await storage.claim_job("leased", lease_duration_seconds=60)


async def test_job_c_02_lease_expiry_reclaim(
    storage: PostgresJobStorage,
) -> None:
    """JOB-C-02 extension: the lease expires and a fresh claim wins."""
    await storage.save_jobs([{"job_id": "expiring", "payload": {}}])
    assert await storage.claim_job("expiring", lease_duration_seconds=0)
    await asyncio.sleep(0.05)
    assert await storage.claim_job("expiring", lease_duration_seconds=60)


async def test_job_c_03_release_allows_reclaim(
    storage: PostgresJobStorage,
) -> None:
    """JOB-C-03: release_job clears the lease for the next caller."""
    await storage.save_jobs([{"job_id": "reclaim", "payload": {}}])
    assert await storage.claim_job("reclaim")
    await storage.release_job("reclaim")
    assert await storage.claim_job("reclaim")


async def test_job_c_04_save_then_load_round_trip(
    storage: PostgresJobStorage,
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


@pytest.mark.xfail(
    strict=False,
    reason="best-effort perf budget — skipped on slow CI runners",
)
async def test_claim_job_p99_under_50_ms(storage: PostgresJobStorage) -> None:
    """1C §2 perf budget: 16-way claim_job p99 ≤ 50 ms.

    Marked ``xfail(strict=False)`` so that container cold-start latency
    never blocks the suite; passing is a positive signal only.
    """
    jobs = [{"job_id": f"perf_{i}", "payload": {}} for i in range(16)]
    await storage.save_jobs(jobs)
    durations: list[float] = []
    for job in jobs:
        start = time.perf_counter()
        await storage.claim_job(job["job_id"])
        durations.append((time.perf_counter() - start) * 1000.0)
    durations.sort()
    p99_index = max(0, int(len(durations) * 0.99) - 1)
    p99 = durations[p99_index]
    assert p99 <= 50.0, f"p99 {p99:.1f} ms exceeds 50 ms budget"
