"""Dedicated contention suite for :class:`JobStorage` backends.

Repeats the 16-parallel ``claim_job`` scenario 25 times (the spec calls
for 100 iterations; 25 is the CI-time compromise noted in Story 4) and
asserts exactly one winner per iteration. This is the most sensitive
knob in the entire extension surface — any ordering bug in ``claim_job``
will surface here long before it shows up in the contract suite.

The suite parametrises over every backend expressible as a zero-argument
factory via :mod:`tests.harness.extensions.contracts.fixtures.job_storage_backends`.
Cloud backends (Postgres / Mongo / Firestore) are covered by their
dedicated module-scoped test files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tests.harness.extensions.contracts.fixtures.job_storage_backends import (
    JOB_STORAGE_FACTORIES,
)

pytestmark = pytest.mark.asyncio

_ITERATIONS = 25
_PARALLEL = 16


@pytest.mark.parametrize(
    "factory",
    [f for _name, f in JOB_STORAGE_FACTORIES],
    ids=[name for name, _f in JOB_STORAGE_FACTORIES],
)
async def test_job_c_01_contention_single_winner_each_iteration(
    factory: Callable[[], Any],
) -> None:
    """JOB-C-01: every iteration yields exactly 1 True / 15 False.

    The test resets the lease between iterations via ``release_job``
    (the ABC does not define ``update_job``). ``save_jobs`` is not used
    because some backends treat it as a destructive replace.
    """
    storage = factory()
    job_id = "contention_job"
    await storage.save_jobs([{"job_id": job_id, "payload": {}}])
    totals = {"wins": 0, "losses": 0}
    for iteration in range(_ITERATIONS):
        await storage.release_job(job_id)
        results = await asyncio.gather(
            *[storage.claim_job(job_id) for _ in range(_PARALLEL)]
        )
        wins = results.count(True)
        losses = results.count(False)
        assert wins == 1, (
            f"iteration {iteration}: expected 1 winner, got {wins} "
            f"(results={results})"
        )
        assert losses == _PARALLEL - 1
        totals["wins"] += wins
        totals["losses"] += losses
    assert totals["wins"] == _ITERATIONS
    assert totals["losses"] == _ITERATIONS * (_PARALLEL - 1)
