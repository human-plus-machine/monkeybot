"""Integration tests for :class:`S3IdentitySource` using ``moto`` + ``aioboto3``.

Skipped cleanly when ``aioboto3``/``moto`` are missing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("aioboto3")
pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from src.core.harness.events import Principal  # noqa: E402
from src.core.harness.extensions import IdentityNotFound, S3IdentitySource  # noqa: E402
from src.core.harness.extensions._aws_clients import reset  # noqa: E402
from src.core.harness.extensions.values import MemoryPatch  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def source() -> AsyncIterator[S3IdentitySource]:
    bucket = f"test-identity-{uuid.uuid4().hex[:8]}"
    with mock_aws():
        import aioboto3

        session = aioboto3.Session(region_name="us-east-1")
        async with session.client("s3") as client:
            await client.create_bucket(Bucket=bucket)
            for name in ("SOUL", "IDENTITY", "USER", "INDEX", "RULES", "MEMORY", "HEARTBEAT"):
                await client.put_object(
                    Bucket=bucket,
                    Key=f"identity/alice/{name}.md",
                    Body=f"{name.lower()}-body".encode(),
                )
        backend = S3IdentitySource(bucket=bucket, prefix="identity", region="us-east-1")
        try:
            yield backend
        finally:
            reset()


async def test_s3_load_known_principal(source: S3IdentitySource) -> None:
    """Loading an existing principal returns a fully populated identity."""
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.principal_id == "alice"
    assert identity.soul == "soul-body"
    assert identity.source_backend == "s3"


async def test_s3_missing_principal_returns_empty_or_raises(
    source: S3IdentitySource,
) -> None:
    """Missing principals either raise or return an empty identity — both count as "not found"."""
    try:
        identity = await source.load(principal=Principal(kind="user", id="nobody"))
    except IdentityNotFound:
        return
    assert identity.soul == ""
    assert identity.rules == ""


async def test_s3_write_memory_round_trip(source: S3IdentitySource) -> None:
    """``write_memory`` replaces MEMORY.md and the next load sees the new value."""
    await source.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
    )
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.memory == "hello"
