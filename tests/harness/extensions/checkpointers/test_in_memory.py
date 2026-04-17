"""Direct unit tests for :class:`InMemoryCheckpointer` (ABC-based path).

Also exercised via the contract suite; these tests cover the in-memory-specific
``gc`` semantics that the generic contract does not mandate.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.core.harness.extensions import CheckpointMissing
from src.core.harness.extensions.checkpointers import InMemoryCheckpointer

pytestmark = pytest.mark.asyncio


async def test_write_returns_ref_with_populated_fields() -> None:
    ckpt = InMemoryCheckpointer()
    ref = await ckpt.write("s", {"k": "v"}, reason="manual")
    assert ref.session_id == "s"
    assert ref.reason == "manual"
    assert ref.bytes > 0
    assert ref.uri.startswith("memory:///s/")
    assert ref.uri.endswith(ref.checkpoint_id)


async def test_read_latest_matches_last_write() -> None:
    ckpt = InMemoryCheckpointer()
    await ckpt.write("s", {"v": 1})
    await ckpt.write("s", {"v": 2})
    assert await ckpt.read("s") == {"v": 2}


async def test_read_missing_id_raises() -> None:
    ckpt = InMemoryCheckpointer()
    await ckpt.write("s", {"v": 1})
    with pytest.raises(CheckpointMissing):
        await ckpt.read("s", "does-not-exist")


async def test_list_newest_first_and_limit() -> None:
    ckpt = InMemoryCheckpointer()
    refs = [await ckpt.write("s", {"i": i}) for i in range(4)]
    listed = await ckpt.list("s", limit=2)
    assert [r.checkpoint_id for r in listed] == [
        refs[-1].checkpoint_id,
        refs[-2].checkpoint_id,
    ]


async def test_delete_session_clears() -> None:
    ckpt = InMemoryCheckpointer()
    await ckpt.write("s", {"v": 1})
    await ckpt.delete_session("s")
    assert await ckpt.read("s") is None
    assert await ckpt.list("s") == []


async def test_concurrent_writes_are_distinct() -> None:
    ckpt = InMemoryCheckpointer()
    refs = await asyncio.gather(*[ckpt.write("s", {"i": i}) for i in range(100)])
    ids = {r.checkpoint_id for r in refs}
    assert len(ids) == 100


async def test_monotonic_ids() -> None:
    ckpt = InMemoryCheckpointer()
    a = await ckpt.write("s", {"i": 1})
    b = await ckpt.write("s", {"i": 2})
    assert a.checkpoint_id < b.checkpoint_id


async def test_gc_purges_everything_when_zero_age() -> None:
    ckpt = InMemoryCheckpointer()
    await ckpt.write("s", {"v": 1})
    await ckpt.write("s", {"v": 2})
    removed = await ckpt.gc(timedelta(seconds=0))
    assert removed == 2
    assert await ckpt.list("s") == []


async def test_gc_keeps_fresh_entries() -> None:
    ckpt = InMemoryCheckpointer()
    await ckpt.write("s", {"v": 1})
    removed = await ckpt.gc(timedelta(days=9999))
    assert removed == 0
    assert len(await ckpt.list("s")) == 1


async def test_large_payload_round_trip() -> None:
    ckpt = InMemoryCheckpointer()
    payload = {"blob": "a" * 1_000_000}
    ref = await ckpt.write("s", payload)
    assert await ckpt.read("s", ref.checkpoint_id) == payload
