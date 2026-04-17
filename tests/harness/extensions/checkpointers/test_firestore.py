"""Unit tests for :class:`FirestoreCheckpointer` using a fake Firestore client.

The fake replicates only the subset of ``google.cloud.firestore.Client`` that
``FirestoreCheckpointer`` exercises (``collection``, ``document``, ``set``,
``where``, ``order_by``, ``limit``, ``stream``, ``get``, ``delete``). It lets
the contract invariants run without the optional dependency installed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from src.core.harness.extensions import CheckpointMissing
from src.core.harness.extensions.checkpointers import FirestoreCheckpointer

pytestmark = pytest.mark.asyncio


class _FakeSnapshot:
    """Stand-in for ``google.cloud.firestore.DocumentSnapshot``."""

    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data
        self.exists = data is not None
        self.reference = _FakeDocumentRef(doc_id, None)

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _FakeDocumentRef:
    def __init__(self, doc_id: str, store: dict[str, dict[str, Any]] | None) -> None:
        self.id = doc_id
        self._store = store

    def set(self, data: dict[str, Any]) -> None:
        if self._store is None:
            raise AssertionError("detached document ref")
        self._store[self.id] = dict(data)

    def get(self) -> _FakeSnapshot:
        if self._store is None:
            return _FakeSnapshot(self.id, None)
        return _FakeSnapshot(self.id, self._store.get(self.id))

    def delete(self) -> None:
        if self._store is None:
            return
        self._store.pop(self.id, None)


class _FakeQuery:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store
        self._filters: list[tuple[str, str, Any]] = []
        self._order_field: str | None = None
        self._order_desc = False
        self._limit: int | None = None

    def where(self, field: str, op: str, value: Any) -> _FakeQuery:
        self._filters.append((field, op, value))
        return self

    def order_by(self, field: str, direction: str = "ASCENDING") -> _FakeQuery:
        self._order_field = field
        self._order_desc = direction.upper() == "DESCENDING"
        return self

    def limit(self, n: int) -> _FakeQuery:
        self._limit = n
        return self

    def stream(self) -> Iterable[_FakeSnapshot]:
        rows: list[tuple[str, dict[str, Any]]] = []
        for doc_id, data in self._store.items():
            ok = True
            for field, op, value in self._filters:
                if op != "==" or data.get(field) != value:
                    ok = False
                    break
            if ok:
                rows.append((doc_id, data))
        if self._order_field is not None:
            rows.sort(
                key=lambda item: item[1].get(self._order_field),  # type: ignore[arg-type,return-value]
                reverse=self._order_desc,
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        snapshots = [_FakeSnapshot(doc_id, data) for doc_id, data in rows]
        for snap in snapshots:
            snap.reference = _FakeDocumentRef(snap.id, self._store)
        return snapshots


class _FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def document(self, doc_id: str) -> _FakeDocumentRef:
        return _FakeDocumentRef(doc_id, self._store)

    def where(self, field: str, op: str, value: Any) -> _FakeQuery:
        return _FakeQuery(self._store).where(field, op, value)

    def order_by(self, field: str, direction: str = "ASCENDING") -> _FakeQuery:
        return _FakeQuery(self._store).order_by(field, direction)

    def limit(self, n: int) -> _FakeQuery:
        return _FakeQuery(self._store).limit(n)

    def stream(self) -> Iterable[_FakeSnapshot]:
        return _FakeQuery(self._store).stream()


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}

    def collection(self, name: str) -> _FakeCollection:
        store = self._collections.setdefault(name, {})
        return _FakeCollection(store)


@pytest.fixture
def fake_firestore(monkeypatch: pytest.MonkeyPatch) -> FirestoreCheckpointer:
    ckpt = FirestoreCheckpointer(project_id="test-project", collection="checkpoints")
    ckpt._client = _FakeFirestoreClient()  # type: ignore[attr-defined]
    return ckpt


async def test_ckpt_c_01_and_02_write_read_latest(fake_firestore: FirestoreCheckpointer) -> None:
    a = await fake_firestore.write("s", {"v": 1})
    b = await fake_firestore.write("s", {"v": 2})
    assert a.checkpoint_id != b.checkpoint_id
    assert a.checkpoint_id < b.checkpoint_id
    assert await fake_firestore.read("s") == {"v": 2}


async def test_ckpt_c_03_read_by_id(fake_firestore: FirestoreCheckpointer) -> None:
    ref = await fake_firestore.write("s", {"v": 42})
    assert await fake_firestore.read("s", ref.checkpoint_id) == {"v": 42}


async def test_ckpt_c_04_list_newest_first(fake_firestore: FirestoreCheckpointer) -> None:
    refs = [await fake_firestore.write("s", {"i": i}) for i in range(4)]
    listed = await fake_firestore.list("s", limit=3)
    assert [r.checkpoint_id for r in listed] == [
        refs[-1].checkpoint_id,
        refs[-2].checkpoint_id,
        refs[-3].checkpoint_id,
    ]


async def test_ckpt_c_05_delete_session(fake_firestore: FirestoreCheckpointer) -> None:
    ref = await fake_firestore.write("s", {"v": 1})
    await fake_firestore.delete_session("s")
    assert await fake_firestore.read("s") is None
    with pytest.raises(CheckpointMissing):
        await fake_firestore.read("s", ref.checkpoint_id)


async def test_ref_uri_includes_project_and_collection(
    fake_firestore: FirestoreCheckpointer,
) -> None:
    ref = await fake_firestore.write("s", {"v": 1})
    assert ref.uri.startswith("firestore://test-project/checkpoints/")
    assert ref.bytes > 0
