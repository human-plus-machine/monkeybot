"""SLO gate for :class:`PostgresCheckpointer` write throughput.

Runs 10 writes/sec for 30 seconds against 64 KB payloads and asserts the p95
write latency stays below 80 ms (1C §2 SLO-B1). Disabled by default — opt in
by exporting ``HARNESS_PERF=1``; additionally skipped if the optional deps
or Docker are unavailable.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("orjson")
pytest.importorskip("testcontainers.postgres")

if os.environ.get("HARNESS_PERF", "0") != "1":
    pytest.skip("set HARNESS_PERF=1 to run perf gate", allow_module_level=True)

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
    env_name = f"CKPT_DSN_PERF_{uuid.uuid4().hex[:6].upper()}"
    os.environ[env_name] = postgres_container
    backend = PostgresCheckpointer(
        dsn_env=env_name,
        schema_name=f"ckpt_perf_{uuid.uuid4().hex[:8]}",
        pool_max_size=10,
    )
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_slo_b1_write_p95(ckpt: PostgresCheckpointer) -> None:
    payload = {"blob": "x" * 64_000}
    semaphore = asyncio.Semaphore(10)
    latencies: list[float] = []

    async def one_write() -> None:
        async with semaphore:
            start = time.perf_counter()
            await ckpt.write("perf-session", payload)
            latencies.append((time.perf_counter() - start) * 1000.0)

    total_seconds = 30
    rps = 10
    tasks = []
    for _second in range(total_seconds):
        for _ in range(rps):
            tasks.append(asyncio.create_task(one_write()))
        await asyncio.sleep(1.0)
    await asyncio.gather(*tasks)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    assert p95 <= 80.0, (
        f"p95 write latency {p95:.1f} ms exceeds SLO-B1 (mean={statistics.mean(latencies):.1f})"
    )
