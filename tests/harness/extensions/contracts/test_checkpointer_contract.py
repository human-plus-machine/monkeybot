"""Contract suite invariants for every :class:`Checkpointer` backend.

IDs map to ``CKPT-C-01`` … ``CKPT-C-07`` in 1b-contracts.md §11.1.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from src.core.harness.extensions import Checkpointer, CheckpointMissing

from .fixtures.checkpointer_backends import CHECKPOINTER_FACTORIES

pytestmark = pytest.mark.asyncio


def _id_fn(param: tuple[str, Callable[[], Checkpointer]]) -> str:
    return param[0]


@pytest.fixture(params=CHECKPOINTER_FACTORIES, ids=_id_fn)
def checkpointer_factory(
    request: pytest.FixtureRequest,
) -> Callable[[], Checkpointer]:
    """Extend conftest's fixture with the shipped backends from Story 2."""
    _, factory = request.param
    return factory  # type: ignore[no-any-return]


async def test_ckpt_c_01_read_after_write(checkpointer_factory: Callable[[], Checkpointer]) -> None:
    """CKPT-C-01: write returns a ref with monotonic id; read-back is consistent."""
    ckpt = checkpointer_factory()
    ref_a = await ckpt.write("session-1", {"x": 1}, reason="turn_end")
    ref_b = await ckpt.write("session-1", {"x": 2}, reason="turn_end")
    assert ref_a.checkpoint_id != ref_b.checkpoint_id
    assert ref_a.checkpoint_id < ref_b.checkpoint_id
    assert await ckpt.read("session-1", ref_a.checkpoint_id) == {"x": 1}


async def test_ckpt_c_02_read_latest(checkpointer_factory: Callable[[], Checkpointer]) -> None:
    """CKPT-C-02: ``read(session_id, None)`` returns the latest write."""
    ckpt = checkpointer_factory()
    await ckpt.write("s", {"v": 1})
    await ckpt.write("s", {"v": 2})
    await ckpt.write("s", {"v": 3})
    assert await ckpt.read("s") == {"v": 3}


async def test_ckpt_c_03_read_by_id(checkpointer_factory: Callable[[], Checkpointer]) -> None:
    """CKPT-C-03: ``read(session_id, cid)`` returns the exact row."""
    ckpt = checkpointer_factory()
    ref = await ckpt.write("s", {"payload": "value"})
    assert await ckpt.read("s", ref.checkpoint_id) == {"payload": "value"}


async def test_ckpt_c_04_list_newest_first(
    checkpointer_factory: Callable[[], Checkpointer],
) -> None:
    """CKPT-C-04: ``list`` is ordered newest-first and respects ``limit``."""
    ckpt = checkpointer_factory()
    refs = [await ckpt.write("s", {"i": i}) for i in range(5)]
    listed = await ckpt.list("s", limit=3)
    assert len(listed) == 3
    assert [r.checkpoint_id for r in listed] == [
        refs[-1].checkpoint_id,
        refs[-2].checkpoint_id,
        refs[-3].checkpoint_id,
    ]


async def test_ckpt_c_05_delete_session(checkpointer_factory: Callable[[], Checkpointer]) -> None:
    """CKPT-C-05: ``delete_session`` removes every row."""
    ckpt = checkpointer_factory()
    ref = await ckpt.write("s", {"v": 1})
    await ckpt.delete_session("s")
    assert await ckpt.read("s") is None
    with pytest.raises(CheckpointMissing):
        await ckpt.read("s", ref.checkpoint_id)


async def test_ckpt_c_06_concurrent_writes_get_distinct_ids(
    checkpointer_factory: Callable[[], Checkpointer],
) -> None:
    """CKPT-C-06: 100 parallel writes produce 100 distinct checkpoint ids."""
    ckpt = checkpointer_factory()
    refs = await asyncio.gather(*[ckpt.write("s", {"i": i}) for i in range(100)])
    ids = {ref.checkpoint_id for ref in refs}
    assert len(ids) == 100


async def test_ckpt_c_07_large_payload_round_trip(
    checkpointer_factory: Callable[[], Checkpointer],
) -> None:
    """CKPT-C-07: a ~1 MB payload round-trips identically."""
    ckpt = checkpointer_factory()
    payload = {"blob": "a" * 1_000_000}
    ref = await ckpt.write("s", payload)
    assert await ckpt.read("s", ref.checkpoint_id) == payload
