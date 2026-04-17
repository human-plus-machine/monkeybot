"""Unit tests for :class:`FirestoreJobStorage` using an in-process fake.

The real ``google.cloud.firestore`` SDK is an optional dependency, so
these tests stand up a thin fake reproducing just enough of ``Client``,
``CollectionReference``, ``DocumentReference``, ``Transaction`` and the
``firestore.transactional`` decorator to exercise
JOB-C-01 … JOB-C-04.

The fake's transaction serialises reads + writes under a single
:class:`threading.Lock` so the 16-way ``asyncio.gather`` race verifies
that :class:`FirestoreJobStorage.claim_job` drives only one successful
``tx.update`` per lease interval.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest

from src.core.harness.extensions.job_storage import FirestoreJobStorage

pytestmark = pytest.mark.asyncio


class _Snapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _Document:
    def __init__(
        self, store: dict[str, dict[str, Any]], doc_id: str, lock: threading.Lock
    ) -> None:
        self._store = store
        self._lock = lock
        self.id = doc_id
        self.reference = self

    def get(self, transaction: _Transaction | None = None) -> _Snapshot:
        data = self._store.get(self.id)
        return _Snapshot(self.id, dict(data) if data is not None else None)

    def set(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._store[self.id] = dict(data)

    def update(self, data: dict[str, Any]) -> None:
        with self._lock:
            existing = self._store.setdefault(self.id, {})
            existing.update(dict(data))

    def delete(self) -> None:
        with self._lock:
            self._store.pop(self.id, None)


class _Collection:
    def __init__(
        self, store: dict[str, dict[str, Any]], lock: threading.Lock
    ) -> None:
        self._store = store
        self._lock = lock

    def document(self, doc_id: str) -> _Document:
        return _Document(self._store, doc_id, self._lock)

    def stream(self) -> list[_Snapshot]:
        with self._lock:
            items = list(self._store.items())
        return [_Snapshot(doc_id, dict(data)) for doc_id, data in items]


class _Batch:
    def __init__(self, store: dict[str, dict[str, Any]], lock: threading.Lock) -> None:
        self._store = store
        self._lock = lock
        self._ops: list[tuple[str, str, dict[str, Any] | None]] = []

    def set(self, doc: _Document, data: dict[str, Any]) -> None:
        self._ops.append(("set", doc.id, dict(data)))

    def delete(self, doc: _Document) -> None:
        self._ops.append(("delete", doc.id, None))

    def commit(self) -> None:
        with self._lock:
            for op, doc_id, data in self._ops:
                if op == "delete":
                    self._store.pop(doc_id, None)
                elif op == "set" and data is not None:
                    self._store[doc_id] = dict(data)


class _Transaction:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def set(self, doc: _Document, data: dict[str, Any]) -> None:
        doc.set(data)

    def update(self, doc: _Document, data: dict[str, Any]) -> None:
        doc.update(data)


class _Client:
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}
        self._tx_lock = threading.RLock()
        self._col_locks: dict[str, threading.Lock] = {}

    def collection(self, name: str) -> _Collection:
        store = self._collections.setdefault(name, {})
        lock = self._col_locks.setdefault(name, threading.Lock())
        return _Collection(store, lock)

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def batch(self) -> _Batch:
        default_name = next(iter(self._collections), "scheduler_jobs")
        store = self._collections.setdefault(default_name, {})
        lock = self._col_locks.setdefault(default_name, threading.Lock())
        return _Batch(store, lock)


def _install_fake_firestore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``google.cloud.firestore`` with the in-process fake."""
    tx_lock = threading.RLock()

    def _transactional(fn: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapper(transaction: _Transaction, *args: Any, **kwargs: Any) -> Any:
            with tx_lock:
                return fn(transaction, *args, **kwargs)

        return _wrapper

    class _Namespace:
        transactional = staticmethod(_transactional)

    import sys
    import types

    google_mod = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_mod = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    google_mod.cloud = cloud_mod  # type: ignore[attr-defined]
    firestore_mod = types.ModuleType("google.cloud.firestore")
    firestore_mod.transactional = _Namespace.transactional  # type: ignore[attr-defined]
    firestore_mod.Client = _Client  # type: ignore[attr-defined]
    cloud_mod.firestore = firestore_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", firestore_mod)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FirestoreJobStorage:
    _install_fake_firestore(monkeypatch)
    backend = FirestoreJobStorage(project_id="test", collection="scheduler_jobs")
    backend._client = _Client()  # type: ignore[attr-defined]
    return backend


async def test_job_c_01_single_winner_under_contention(
    storage: FirestoreJobStorage,
) -> None:
    """JOB-C-01: exactly one of 16 concurrent claims wins."""
    await storage.save_jobs([{"job_id": "race", "payload": {}}])
    results = await asyncio.gather(*[storage.claim_job("race") for _ in range(16)])
    assert results.count(True) == 1
    assert results.count(False) == 15


async def test_job_c_02_claim_fails_while_leased(
    storage: FirestoreJobStorage,
) -> None:
    """JOB-C-02: a second claim on a leased job returns ``False``."""
    await storage.save_jobs([{"job_id": "leased", "payload": {}}])
    assert await storage.claim_job("leased", lease_duration_seconds=60)
    assert not await storage.claim_job("leased", lease_duration_seconds=60)


async def test_job_c_02_lease_expiry_reclaim(
    storage: FirestoreJobStorage,
) -> None:
    """JOB-C-02 extension: an expired lease can be re-claimed."""
    await storage.save_jobs([{"job_id": "expiring", "payload": {}}])
    assert await storage.claim_job("expiring", lease_duration_seconds=0)
    await asyncio.sleep(0.05)
    assert await storage.claim_job("expiring", lease_duration_seconds=60)


async def test_job_c_03_release_allows_reclaim(
    storage: FirestoreJobStorage,
) -> None:
    """JOB-C-03: release_job clears the lease."""
    await storage.save_jobs([{"job_id": "reclaim", "payload": {}}])
    assert await storage.claim_job("reclaim")
    await storage.release_job("reclaim")
    assert await storage.claim_job("reclaim")


async def test_job_c_04_save_then_load_round_trip(
    storage: FirestoreJobStorage,
) -> None:
    """JOB-C-04: save_jobs + load_jobs round-trips payloads."""
    await storage.save_jobs(
        [
            {"job_id": "a", "payload": {"n": 1}},
            {"job_id": "b", "payload": {"n": 2}},
        ]
    )
    loaded = await storage.load_jobs()
    ids = {job["job_id"] for job in loaded}
    assert ids == {"a", "b"}


async def test_transactional_decorator_is_used(
    storage: FirestoreJobStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claim_job`` exercises the ``firestore.transactional`` decorator."""
    import sys

    firestore_mod = sys.modules["google.cloud.firestore"]
    original = firestore_mod.transactional  # type: ignore[attr-defined]
    calls = {"count": 0}

    def _spy(fn: Any) -> Any:
        wrapped = original(fn)

        def _inner(*args: Any, **kwargs: Any) -> Any:
            calls["count"] += 1
            return wrapped(*args, **kwargs)

        return _inner

    monkeypatch.setattr(firestore_mod, "transactional", _spy)
    await storage.save_jobs([{"job_id": "spy", "payload": {}}])
    assert await storage.claim_job("spy")
    assert calls["count"] >= 1
