"""Integration tests for :class:`PostgresIdentitySource` via testcontainers.

Skipped when ``asyncpg`` or ``testcontainers`` (or Docker) are missing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("testcontainers")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from src.core.harness.events import Principal  # noqa: E402
from src.core.harness.extensions import IdentityNotFound, PostgresIdentitySource  # noqa: E402
from src.core.harness.extensions._postgres_pool import reset  # noqa: E402
from src.core.harness.extensions.values import MemoryPatch  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def source() -> AsyncIterator[PostgresIdentitySource]:
    schema = f"identity_{uuid.uuid4().hex[:8]}"
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker unavailable: {exc}")
    try:
        dsn = container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
        backend = PostgresIdentitySource(dsn=dsn, schema=schema)
        yield backend
    finally:
        reset()
        container.stop()


async def _seed(source: PostgresIdentitySource, principal_id: str) -> None:
    for name in ("SOUL", "IDENTITY", "USER", "INDEX", "RULES", "MEMORY", "HEARTBEAT"):
        await source.write_memory(
            principal=Principal(kind="user", id=principal_id),
            patch=MemoryPatch(
                target="MEMORY.md" if name == "MEMORY" else "HEARTBEAT.md",
                operation="replace",
                content=f"{name.lower()}-body",
            ),
        ) if name in {"MEMORY", "HEARTBEAT"} else None


async def test_postgres_write_memory_round_trip(source: PostgresIdentitySource) -> None:
    """``write_memory`` followed by ``load`` returns the new content."""
    await source.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
    )
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.memory == "hello"
    assert identity.source_backend == "postgres"


async def test_postgres_missing_principal_raises(source: PostgresIdentitySource) -> None:
    """Loading a principal with no rows raises :class:`IdentityNotFound`."""
    with pytest.raises(IdentityNotFound):
        await source.load(principal=Principal(kind="user", id="nobody"))
