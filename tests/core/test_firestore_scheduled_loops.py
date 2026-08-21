"""Unit tests for scheduled-loop document mapping and Firestore claim races."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.cloud.firestore")

from monkeybot.core.persistence.firestore_scheduled_loops import FirestoreScheduledLoopStore  # noqa: E402
from monkeybot.core.persistence.scheduled_loops import doc_to_scheduled_loop_row  # noqa: E402


def test_doc_to_scheduled_loop_row_roundtrip_fields() -> None:
    row = doc_to_scheduled_loop_row(
        "demo",
        {
            "session_id": "loop-main",
            "status": "active",
            "prompt": "tick plan",
            "interval_ms": 5000,
            "max_ticks": 3,
            "max_runtime_ms": 3600000,
            "skip_if_busy": 1,
            "tick_index": 2,
            "next_tick_at_ms": 100,
            "started_at_ms": 50,
            "last_tick_at_ms": 90,
            "last_error": None,
            "stop_reason": None,
            "tick_in_flight": 0,
            "worker_id": None,
            "claimed_at_ms": None,
        },
    )
    assert row.loop_id == "demo"
    assert row.session_id == "loop-main"
    assert row.max_ticks == 3
    assert row.skip_if_busy is True
    assert row.tick_index == 2


class _FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data or {})


class _FakeTransaction:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store
        self._pending: list[tuple[str, dict[str, Any]]] = []

    def update(self, ref: "_FakeDocRef", fields: dict[str, Any]) -> None:
        self._pending.append((ref.id, fields))

    def commit(self) -> None:
        for doc_id, fields in self._pending:
            cur = dict(self._store.get(doc_id, {}))
            cur.update(fields)
            self._store[doc_id] = cur
        self._pending.clear()


class _FakeDocRef:
    def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
        self.id = doc_id
        self._store = store
        self.reference = self

    async def get(self, transaction: _FakeTransaction | None = None) -> _FakeSnapshot:
        data = self._store.get(self.id)
        return _FakeSnapshot(self.id, None if data is None else dict(data))

    async def update(self, fields: dict[str, Any]) -> None:
        cur = dict(self._store.get(self.id, {}))
        cur.update(fields)
        self._store[self.id] = cur


class _FakeQuery:
    def __init__(
        self, store: dict[str, dict[str, Any]], filters: list[tuple[str, str, Any]]
    ) -> None:
        self._store = store
        self._filters = filters

    def where(self, *, filter: Any) -> "_FakeQuery":  # noqa: A002
        return _FakeQuery(
            self._store,
            [*self._filters, (filter.field_path, filter.op_string, filter.value)],
        )

    async def stream(self):
        for doc_id, data in list(self._store.items()):
            ok = True
            for field_path, op, value in self._filters:
                cur = data.get(field_path)
                if op == "==" and cur != value:
                    ok = False
                    break
                if op == "<" and not (cur is not None and cur < value):
                    ok = False
                    break
                if op == "<=" and not (cur is not None and cur <= value):
                    ok = False
                    break
            if ok:
                # Real Firestore query.stream() yields DocumentSnapshots (id + to_dict).
                snap = _FakeSnapshot(doc_id, dict(data))
                snap.reference = _FakeDocRef(self._store, doc_id)  # type: ignore[attr-defined]
                yield snap


class _FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)

    def where(self, *, filter: Any) -> _FakeQuery:  # noqa: A002
        return _FakeQuery(self._store, []).where(filter=filter)


class _FakeClient:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def collection(self, _name: str) -> _FakeCollection:
        return _FakeCollection(self.docs)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self.docs)

    def batch(self) -> MagicMock:
        raise AssertionError("batch updates must not be used for stale claim release")


def _install_fake_transactional(monkeypatch: pytest.MonkeyPatch) -> None:
    import monkeybot.core.persistence.firestore_scheduled_loops as mod

    def _async_transactional(fn):  # noqa: ANN001
        async def wrapper(txn: _FakeTransaction, *args: Any, **kwargs: Any):
            result = await fn(txn, *args, **kwargs)
            txn.commit()
            return result

        return wrapper

    monkeypatch.setattr(mod.firestore, "async_transactional", _async_transactional)


def _seed_in_flight(
    client: _FakeClient,
    *,
    loop_id: str = "loop-1",
    worker_id: str = "worker-a",
    claimed_at_ms: int = 1_000,
    interval_ms: int = 5_000,
) -> None:
    client.docs[loop_id] = {
        "session_id": "loop-main",
        "status": "active",
        "prompt": "tick",
        "interval_ms": interval_ms,
        "max_ticks": 10,
        "max_runtime_ms": None,
        "skip_if_busy": 1,
        "tick_index": 0,
        "next_tick_at_ms": claimed_at_ms,
        "started_at_ms": claimed_at_ms,
        "last_tick_at_ms": None,
        "last_error": None,
        "stop_reason": None,
        "tick_in_flight": 1,
        "worker_id": worker_id,
        "claimed_at_ms": claimed_at_ms,
    }


@pytest.mark.asyncio
async def test_firestore_defer_tick_skips_when_claim_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transactional(monkeypatch)
    client = _FakeClient()
    _seed_in_flight(client, worker_id="worker-new", claimed_at_ms=9_000)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    deferred = await store.defer_tick("loop-1", worker_id="worker-old", reason="session busy")

    assert deferred is False
    row = client.docs["loop-1"]
    assert row["worker_id"] == "worker-new"
    assert row["tick_in_flight"] == 1
    assert row["claimed_at_ms"] == 9_000


@pytest.mark.asyncio
async def test_firestore_defer_tick_releases_own_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transactional(monkeypatch)
    client = _FakeClient()
    _seed_in_flight(client, worker_id="worker-a", claimed_at_ms=1_000, interval_ms=5_000)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    deferred = await store.defer_tick("loop-1", worker_id="worker-a", reason="session busy")

    assert deferred is True
    row = client.docs["loop-1"]
    assert row["worker_id"] is None
    assert row["tick_in_flight"] == 0
    assert row["claimed_at_ms"] is None
    assert row["last_error"] == "session busy"
    assert row["next_tick_at_ms"] >= 5_000


@pytest.mark.asyncio
async def test_firestore_release_stale_skips_renewed_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query sees stale claimed_at; transactional re-check sees renewed lease."""
    _install_fake_transactional(monkeypatch)
    client = _FakeClient()
    _seed_in_flight(client, worker_id="worker-a", claimed_at_ms=1)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    import monkeybot.core.persistence.firestore_scheduled_loops as mod

    original_release = store._release_one_stale_claim

    async def _renew_then_release(loop_id: str, cutoff: int) -> bool:
        client.docs[loop_id]["claimed_at_ms"] = cutoff + 50_000
        return await original_release(loop_id, cutoff)

    monkeypatch.setattr(store, "_release_one_stale_claim", _renew_then_release)
    # now_ms = 100_000; cutoff = 99_000 → claimed_at_ms=1 matches the query.
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)

    released = await store.release_stale_claims(stale_after_ms=1_000)
    assert released == 0
    row = client.docs["loop-1"]
    assert row["tick_in_flight"] == 1
    assert row["worker_id"] == "worker-a"
    assert row["claimed_at_ms"] == 149_000


@pytest.mark.asyncio
async def test_firestore_release_stale_clears_truly_stale_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transactional(monkeypatch)
    client = _FakeClient()
    _seed_in_flight(client, worker_id="worker-a", claimed_at_ms=1)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    import monkeybot.core.persistence.firestore_scheduled_loops as mod

    monkeypatch.setattr(mod.time, "time", lambda: 100.0)
    released = await store.release_stale_claims(stale_after_ms=1_000)
    assert released == 1
    row = client.docs["loop-1"]
    assert row["tick_in_flight"] == 0
    assert row["worker_id"] is None
    assert row["claimed_at_ms"] is None
