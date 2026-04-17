"""Integration tests for :class:`MongoIdentitySource` via testcontainers.

Skipped when ``motor``/``testcontainers`` (or Docker) are missing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("motor")
pytest.importorskip("testcontainers")

from testcontainers.mongodb import MongoDbContainer  # noqa: E402

from src.core.harness.events import Principal  # noqa: E402
from src.core.harness.extensions import IdentityNotFound, MongoIdentitySource  # noqa: E402
from src.core.harness.extensions._mongo_client import reset  # noqa: E402
from src.core.harness.extensions.values import MemoryPatch  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def source() -> AsyncIterator[MongoIdentitySource]:
    try:
        container = MongoDbContainer("mongo:7")
        container.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker unavailable: {exc}")
    try:
        uri = container.get_connection_url()
        db = f"identity_{uuid.uuid4().hex[:8]}"
        backend = MongoIdentitySource(uri=uri, db=db)
        yield backend
    finally:
        reset()
        container.stop()


async def test_mongo_write_then_load(source: MongoIdentitySource) -> None:
    """Writing MEMORY.md and then loading yields the new content."""
    await source.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
    )
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.memory == "hello"
    assert identity.source_backend == "mongo"


async def test_mongo_missing_principal_raises(source: MongoIdentitySource) -> None:
    """Loading a principal with no document raises :class:`IdentityNotFound`."""
    with pytest.raises(IdentityNotFound):
        await source.load(principal=Principal(kind="user", id="nobody"))
