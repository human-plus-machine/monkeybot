"""Contract suite invariants for every :class:`JobStorage` backend.

IDs map to ``JOB-C-01`` … ``JOB-C-04`` in 1b-contracts.md §11.1.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from src.core.harness.extensions import JobStorage

pytestmark = pytest.mark.asyncio


async def test_job_c_01_single_winner_under_contention(
    job_storage_factory: Callable[[], JobStorage],
) -> None:
    """JOB-C-01: exactly one claimer wins when many race for the same job."""
    storage = job_storage_factory()
    await storage.save_jobs([{"job_id": "race", "payload": {}}])
    results = await asyncio.gather(*[storage.claim_job("race") for _ in range(16)])
    assert results.count(True) == 1
    assert results.count(False) == 15


async def test_job_c_02_claim_fails_while_leased(
    job_storage_factory: Callable[[], JobStorage],
) -> None:
    """JOB-C-02: a leased job cannot be claimed by a second caller."""
    storage = job_storage_factory()
    await storage.save_jobs([{"job_id": "leased", "payload": {}}])
    assert await storage.claim_job("leased", lease_duration_seconds=60)
    assert not await storage.claim_job("leased", lease_duration_seconds=60)


async def test_job_c_03_release_allows_reclaim(
    job_storage_factory: Callable[[], JobStorage],
) -> None:
    """JOB-C-03: releasing a lease lets the next caller claim it."""
    storage = job_storage_factory()
    await storage.save_jobs([{"job_id": "reclaim", "payload": {}}])
    assert await storage.claim_job("reclaim")
    await storage.release_job("reclaim")
    assert await storage.claim_job("reclaim")


async def test_job_c_04_save_then_load_round_trip(
    job_storage_factory: Callable[[], JobStorage],
) -> None:
    """JOB-C-04: ``save_jobs`` followed by ``load_jobs`` round-trips the payload."""
    storage = job_storage_factory()
    jobs = [
        {"job_id": "a", "payload": {"n": 1}},
        {"job_id": "b", "payload": {"n": 2}},
    ]
    await storage.save_jobs(jobs)
    loaded = await storage.load_jobs()
    ids = {job["job_id"] for job in loaded}
    assert ids == {"a", "b"}


async def test_save_job_preserves_unrelated_jobs(
    job_storage_factory: Callable[[], JobStorage],
) -> None:
    """Single-job saves must not replace the whole persisted job list."""
    storage = job_storage_factory()
    await storage.save_jobs([
        {"job_id": "a", "payload": {"n": 1}, "status": "pending"},
        {"job_id": "b", "payload": {"n": 2}, "status": "pending"},
    ])

    await storage.save_job({"job_id": "a", "payload": {"n": 3}, "status": "completed"})
    loaded = await storage.load_jobs()
    by_id = {job["job_id"]: job for job in loaded}

    assert set(by_id) == {"a", "b"}
    assert by_id["a"]["payload"] == {"n": 3}
    assert by_id["a"]["status"] == "completed"
    assert by_id["b"]["payload"] == {"n": 2}
    assert by_id["b"]["status"] == "pending"
