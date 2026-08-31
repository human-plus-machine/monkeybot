"""Unit tests for scheduled-loop document mapping and Firestore claim races."""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.cloud.firestore")

from monkeybot.core.persistence.firestore_scheduled_loops import FirestoreScheduledLoopStore  # noqa: E402


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
    def __init__(
        self,
        store: dict[str, dict[str, Any]],
        doc_id: str,
        *,
        get_in_txn_hook: Callable[[], None] | None = None,
    ) -> None:
        self.id = doc_id
        self._store = store
        self._get_in_txn_hook = get_in_txn_hook

    async def get(self, transaction: _FakeTransaction | None = None) -> _FakeSnapshot:
        if transaction is not None and self._get_in_txn_hook is not None:
            self._get_in_txn_hook()
        data = self._store.get(self.id)
        return _FakeSnapshot(self.id, None if data is None else dict(data))


class _FakeQuery:
    def __init__(
        self,
        store: dict[str, dict[str, Any]],
        filters: list[tuple[str, str, Any]],
        order_field: str | None = None,
    ) -> None:
        self._store = store
        self._filters = filters
        self._order_field = order_field

    def where(self, *, filter: Any) -> "_FakeQuery":  # noqa: A002
        return _FakeQuery(
            self._store,
            [*self._filters, (filter.field_path, filter.op_string, filter.value)],
            self._order_field,
        )

    def order_by(self, field: str, direction: Any = None) -> "_FakeQuery":
        return _FakeQuery(self._store, self._filters, field)

    async def stream(self):
        matched = []
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
                matched.append((doc_id, data))
        if self._order_field is not None:
            matched.sort(key=lambda kv: kv[1].get(self._order_field))
        for doc_id, data in matched:
            yield _FakeSnapshot(doc_id, dict(data))


class _FakeCollection:
    def __init__(
        self,
        store: dict[str, dict[str, Any]],
        get_in_txn_hooks: dict[str, Callable[[], None]],
    ) -> None:
        self._store = store
        self._get_in_txn_hooks = get_in_txn_hooks

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(
            self._store,
            doc_id,
            get_in_txn_hook=self._get_in_txn_hooks.get(doc_id),
        )

    def where(self, *, filter: Any) -> _FakeQuery:  # noqa: A002
        return _FakeQuery(self._store, []).where(filter=filter)

    def order_by(self, field: str, direction: Any = None) -> _FakeQuery:
        return _FakeQuery(self._store, []).order_by(field, direction)


class _FakeClient:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.get_in_txn_hooks: dict[str, Callable[[], None]] = {}

    def collection(self, _name: str) -> _FakeCollection:
        return _FakeCollection(self.docs, self.get_in_txn_hooks)

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
async def test_firestore_defer_tick_rejects_invalid_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transactional(monkeypatch)
    client = _FakeClient()
    _seed_in_flight(client, worker_id="worker-a", claimed_at_ms=1_000, interval_ms=0)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    deferred = await store.defer_tick("loop-1", worker_id="worker-a", reason="session busy")

    assert deferred is False
    row = client.docs["loop-1"]
    assert row["worker_id"] == "worker-a"
    assert row["tick_in_flight"] == 1


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

    # now_ms = 100_000; cutoff = 99_000 → claimed_at_ms=1 matches the query.
    cutoff = 99_000
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)
    client.get_in_txn_hooks["loop-1"] = lambda: client.docs["loop-1"].__setitem__(
        "claimed_at_ms", cutoff + 50_000
    )

    released = await store.release_stale_claims(stale_after_ms=1_000)
    assert released == 0
    row = client.docs["loop-1"]
    assert row["tick_in_flight"] == 1
    assert row["worker_id"] == "worker-a"
    assert row["claimed_at_ms"] == 149_000


def _seed_due(
    client: _FakeClient,
    loop_id: str,
    *,
    interval_ms: int = 5_000,
    next_tick_at_ms: int = 100,
    started_at_ms: int = 50,
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
        "next_tick_at_ms": next_tick_at_ms,
        "started_at_ms": started_at_ms,
        "last_tick_at_ms": None,
        "last_error": None,
        "stop_reason": None,
        "tick_in_flight": 0,
        "worker_id": None,
        "claimed_at_ms": None,
    }


@pytest.mark.asyncio
async def test_firestore_list_due_skips_malformed_doc() -> None:
    """A single doc with an invalid interval_ms must not stall list_due for
    every other loop (would otherwise raise inside the scheduler poll loop
    before any due loop gets claimed)."""
    client = _FakeClient()
    _seed_due(client, "loop-good")
    _seed_due(client, "loop-bad", interval_ms=0)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    due = await store.list_due(now_ms=1_000)

    assert [row.loop_id for row in due] == ["loop-good"]


@pytest.mark.asyncio
async def test_firestore_list_all_skips_malformed_doc() -> None:
    client = _FakeClient()
    _seed_due(client, "loop-good", started_at_ms=200)
    _seed_due(client, "loop-bad", interval_ms=0, started_at_ms=100)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    rows = await store.list_all()

    assert [row.loop_id for row in rows] == ["loop-good"]


@pytest.mark.asyncio
async def test_firestore_get_returns_none_for_malformed_doc() -> None:
    client = _FakeClient()
    _seed_due(client, "loop-bad", interval_ms=0)
    store = FirestoreScheduledLoopStore(client, prefix="t")  # type: ignore[arg-type]

    assert await store.get("loop-bad") is None


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
