"""JOB-C-01 … JOB-C-04 invariants for :class:`JSONFileJobStorage`.

The JSON-file backend relies on ``filelock`` (cross-process) plus an
:class:`asyncio.Lock` (in-process) to serialise ``claim_job``. These
tests hit both paths:

* The 16-way ``asyncio.gather`` race covers the in-process asyncio lock.
* The contention suite (``test_claim_job_contention.py``) repeats the
  scenario multiple times to flush out ordering bugs.

See 1b-contracts.md §11 for the invariant IDs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("filelock")

from src.core.harness.extensions.job_storage import JSONFileJobStorage  # noqa: E402

pytestmark = pytest.mark.asyncio


async def test_job_c_01_single_winner_under_contention(tmp_path: Path) -> None:
    """JOB-C-01: exactly one of 16 concurrent claim_job calls wins.

    In-process contention is arbitrated by an :class:`asyncio.Lock` on
    the backend instance; the file lock still protects against
    cross-process races at the OS level.
    """
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    await storage.save_jobs([{"job_id": "race", "payload": {}}])
    results = await asyncio.gather(*[storage.claim_job("race") for _ in range(16)])
    assert results.count(True) == 1
    assert results.count(False) == 15


async def test_job_c_02_claim_fails_while_leased(tmp_path: Path) -> None:
    """JOB-C-02: a leased job cannot be claimed by a second caller."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    await storage.save_jobs([{"job_id": "leased", "payload": {}}])
    assert await storage.claim_job("leased", lease_duration_seconds=60)
    assert not await storage.claim_job("leased", lease_duration_seconds=60)


async def test_job_c_02_lease_expiry_reclaim(tmp_path: Path) -> None:
    """JOB-C-02 extension: a lease expires and a fresh claim succeeds."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    await storage.save_jobs([{"job_id": "expiring", "payload": {}}])
    assert await storage.claim_job("expiring", lease_duration_seconds=0)
    await asyncio.sleep(0.05)
    assert await storage.claim_job("expiring", lease_duration_seconds=60)


async def test_job_c_03_release_allows_reclaim(tmp_path: Path) -> None:
    """JOB-C-03: release_job clears the lease so the next caller wins."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    await storage.save_jobs([{"job_id": "reclaim", "payload": {}}])
    assert await storage.claim_job("reclaim")
    await storage.release_job("reclaim")
    assert await storage.claim_job("reclaim")


async def test_job_c_04_save_then_load_round_trip(tmp_path: Path) -> None:
    """JOB-C-04: save_jobs + load_jobs round-trips payloads by ``job_id``."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    jobs = [
        {"job_id": "a", "payload": {"n": 1}},
        {"job_id": "b", "payload": {"n": 2}},
    ]
    await storage.save_jobs(jobs)
    loaded = await storage.load_jobs()
    by_id = {job["job_id"]: job for job in loaded}
    assert set(by_id) == {"a", "b"}
    assert by_id["a"]["payload"] == {"n": 1}
    assert by_id["b"]["payload"] == {"n": 2}


async def test_save_jobs_replaces_list(tmp_path: Path) -> None:
    """``save_jobs`` replaces the job list (matches ABC §3.3 wording)."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    await storage.save_jobs([{"job_id": "old", "payload": {}}])
    await storage.save_jobs([{"job_id": "new", "payload": {}}])
    loaded = await storage.load_jobs()
    assert {job["job_id"] for job in loaded} == {"new"}


async def test_save_job_preserves_list(tmp_path: Path) -> None:
    """``save_job`` upserts one job without dropping unrelated jobs."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    await storage.save_jobs([
        {"job_id": "old", "payload": {"n": 1}, "status": "pending"},
        {"job_id": "kept", "payload": {"n": 2}, "status": "pending"},
    ])

    await storage.save_job({"job_id": "old", "payload": {"n": 3}, "status": "completed"})
    loaded = await storage.load_jobs()
    by_id = {job["job_id"]: job for job in loaded}

    assert set(by_id) == {"old", "kept"}
    assert by_id["old"]["payload"] == {"n": 3}
    assert by_id["old"]["status"] == "completed"
    assert by_id["kept"]["payload"] == {"n": 2}


async def test_get_job_returns_none_for_missing(tmp_path: Path) -> None:
    """``get_job`` returns ``None`` when the id is not persisted."""
    storage = JSONFileJobStorage(tmp_path / "jobs.json")
    assert await storage.get_job("missing") is None
    await storage.save_jobs([{"job_id": "present", "payload": {"n": 1}}])
    job = await storage.get_job("present")
    assert job is not None
    assert job["payload"] == {"n": 1}
