"""Firestore history store tests (require a running Firestore emulator)."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

pytest.importorskip("google.cloud.firestore")

from google.cloud.firestore import AsyncClient  # noqa: E402

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.firestore import FirestoreHistoryStore, firestore_summary_doc_id
from monkeybot.core.types.content_blocks import Text


def _emulator_host() -> str | None:
    return os.environ.get("FIRESTORE_EMULATOR_HOST")


@pytest.fixture
def require_emulator() -> None:
    if not _emulator_host():
        pytest.skip("FIRESTORE_EMULATOR_HOST not set")



# ---------------------------------------------------------------------------
# Pure-logic tests — no emulator required.
# ---------------------------------------------------------------------------


def test_summary_doc_id_is_deterministic_and_slash_safe() -> None:
    """Regression for PR #179 review: a raw thread_id appended into the
    summary doc id broke the moment it contained '/' (Firestore forbids '/'
    in a document id, and thread_id — accepted verbatim from --session <id>)
    is never validated against that. Hashing scope+thread_id together
    produces a fixed-length hex id that can never contain '/'.
    """
    store = FirestoreHistoryStore(None, "prefix", "agent-a")  # type: ignore[arg-type]
    doc_id = store._summary_doc_id("weird/thread/id")
    assert "/" not in doc_id
    assert doc_id == store._summary_doc_id("weird/thread/id")


def test_summary_doc_id_differs_by_scope_and_thread_id() -> None:
    a = FirestoreHistoryStore(None, "prefix", "agent-a")  # type: ignore[arg-type]
    b = FirestoreHistoryStore(None, "prefix", "agent-b")  # type: ignore[arg-type]
    assert a._summary_doc_id("t1") != b._summary_doc_id("t1")
    assert a._summary_doc_id("t1") != a._summary_doc_id("t2")


def test_summary_doc_id_matches_public_function() -> None:
    """Regression for PR #179 review: the operator migration recipe
    (docs/migrations/agent-scope-namespacing.md) told operators to compute
    sha256(agent_scope + thread_id), which didn't match the real formula
    (a NUL-separated hash) — following it would have written a summary doc
    at the wrong id, reproducing the exact stale/duplicate-summary bug the
    migration exists to fix. The store's internal id must always equal the
    public, importable firestore_summary_doc_id() operators actually call
    (see the doc), so the two can never drift again.
    """
    store = FirestoreHistoryStore(None, "prefix", "agent-a")  # type: ignore[arg-type]
    assert store._summary_doc_id("thread-x") == firestore_summary_doc_id("agent-a", "thread-x")


class _FakeDoc:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.id = data.get("thread_id", "doc-id")

    def to_dict(self) -> dict:
        return self._data


class _FakeQuery:
    """Respects limit()/start_after() for real, unlike a query stub that just
    streams everything: list_threads()'s pagination-around-the-filter fix
    (PR #179 review) is invisible to tests unless limit() actually truncates.

    ``order_by`` stays a no-op — callers must hand in ``docs`` pre-sorted the
    way a real Firestore query would return them (newest ``last_message_at``
    first), which every test below does explicitly.
    """

    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs

    def where(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def order_by(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def start_after(self, doc: _FakeDoc) -> _FakeQuery:
        try:
            idx = self._docs.index(doc)
        except ValueError:
            idx = -1
        return _FakeQuery(self._docs[idx + 1 :])

    def limit(self, n: int) -> _FakeQuery:
        return _FakeQuery(self._docs[:n])

    async def stream(self):
        for doc in self._docs:
            yield doc


class _FakeCollection:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs

    def document(self, _doc_id: str):
        raise NotImplementedError

    def where(self, *args, **kwargs) -> _FakeQuery:
        return _FakeQuery(self._docs).where(*args, **kwargs)


class _FakeFirestoreClient:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs

    def collection(self, _name: str) -> _FakeCollection:
        return _FakeCollection(self._docs)


@pytest.mark.asyncio
async def test_list_threads_excludes_subagent_transcripts() -> None:
    """Regression for PR #179 review: filtering only at append() time left
    subagent summary docs written *before* that guard existed still listable
    — a finished subagent could outrank its parent as "newest" and get
    resumed by --continue instead of the actual previous chat. list_threads()
    must filter client-side too, since Firestore has no "does not start with"
    query.
    """
    docs = [
        # Pre-sorted newest-first, matching what a real descending query returns.
        _FakeDoc(
            {
                "thread_id": "subagent:main-thread:abc123",
                "last_message_at": 3,
                "message_count": 1,
                "last_content": "",
            }
        ),
        _FakeDoc(
            {
                "thread_id": "main-thread",
                "last_message_at": 2,
                "message_count": 1,
                "last_content": "",
            }
        ),
    ]
    client = _FakeFirestoreClient(docs)
    store = FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]
    threads = await store.list_threads()
    assert [t.thread_id for t in threads] == ["main-thread"]


@pytest.mark.asyncio
async def test_list_threads_paginates_past_filtered_subagent_at_limit_one() -> None:
    """Regression for PR #179 review: .limit(cap) ran before the client-side
    subagent filter, so --continue's limit=1 returned nothing whenever the
    single newest doc was a subagent summary — even with a perfectly valid
    parent thread ranked right behind it. Must overfetch/paginate instead.
    """
    docs = [
        _FakeDoc(
            {
                "thread_id": "subagent:main-thread:abc123",
                "last_message_at": 3,
                "message_count": 1,
                "last_content": "",
            }
        ),
        _FakeDoc(
            {
                "thread_id": "main-thread",
                "last_message_at": 2,
                "message_count": 1,
                "last_content": "",
            }
        ),
    ]
    client = _FakeFirestoreClient(docs)
    store = FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]
    threads = await store.list_threads(limit=1)
    assert [t.thread_id for t in threads] == ["main-thread"]


@pytest.mark.asyncio
async def test_list_threads_stops_at_cap_without_overfetching_forever() -> None:
    """Sanity check on the pagination loop: once cap real threads are found,
    it must stop rather than keep paginating through the rest of the
    collection.
    """
    docs = [_FakeDoc({"thread_id": f"t{i}", "last_message_at": 100 - i, "message_count": 1, "last_content": ""}) for i in range(5)]
    client = _FakeFirestoreClient(docs)
    store = FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]
    threads = await store.list_threads(limit=2)
    assert [t.thread_id for t in threads] == ["t0", "t1"]


@pytest.mark.asyncio
async def test_list_threads_exhausts_collection_of_only_subagent_docs() -> None:
    """All docs are subagent summaries — must terminate (not loop forever)
    and return an empty list rather than exhausting on an infinite retry.
    """
    docs = [
        _FakeDoc(
            {
                "thread_id": f"subagent:main:{i}",
                "last_message_at": 10 - i,
                "message_count": 1,
                "last_content": "",
            }
        )
        for i in range(3)
    ]
    client = _FakeFirestoreClient(docs)
    store = FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]
    threads = await store.list_threads(limit=5)
    assert threads == []


@pytest_asyncio.fixture
async def firestore_history(require_emulator: None):
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    client = AsyncClient(project="monkeybot-test", database="(default)")
    store = FirestoreHistoryStore(client, prefix)
    yield store
    for collection in (
        store._collection,
        store._threads_collection,
    ):
        async for doc in client.collection(collection).stream():
            await doc.reference.delete()
    close_fn = client.close
    close_fn()


@pytest.mark.asyncio
async def test_firestore_list_threads_uses_threads_collection(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    await store.append("thread-a", Message(role="user", content=[Text(text="hello")]))
    await store.append("thread-b", Message(role="user", content=[Text(text="other")]))
    await store.append("thread-a", Message(role="assistant", content=[Text(text="reply")]))

    threads = await store.list_threads(limit=10)
    assert len(threads) == 2
    by_id = {t.thread_id: t for t in threads}
    assert by_id["thread-a"].message_count == 2
    assert "reply" in by_id["thread-a"].preview
    assert by_id["thread-b"].message_count == 1
    assert threads[0].thread_id == "thread-a"


@pytest.mark.asyncio
async def test_firestore_list_threads_isolated_by_agent_scope(require_emulator: None) -> None:
    """Regression for PR #179 review: agents sharing one Firestore database
    must not see each other's threads via list_threads/load.
    """
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    client = AsyncClient(project="monkeybot-test", database="(default)")
    store_a = FirestoreHistoryStore(client, prefix, "agent-a")
    store_b = FirestoreHistoryStore(client, prefix, "agent-b")
    try:
        await store_a.append("t1", Message(role="user", content=[Text(text="agent a's secret")]))
        await store_b.append("t2", Message(role="user", content=[Text(text="agent b's secret")]))

        threads_a = await store_a.list_threads(limit=10)
        threads_b = await store_b.list_threads(limit=10)

        assert [t.thread_id for t in threads_a] == ["t1"]
        assert [t.thread_id for t in threads_b] == ["t2"]
        assert await store_a.load("t2") == []
        assert await store_b.load("t1") == []
    finally:
        for collection in (store_a._collection, store_a._threads_collection):
            async for doc in client.collection(collection).stream():
                await doc.reference.delete()
        client.close()


@pytest.mark.asyncio
async def test_firestore_reset_rebuilds_thread_summary(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    thread_id = "thread-reset"
    for i in range(5):
        await store.append(thread_id, Message(role="user", content=[Text(text=f"m{i}")]))
    await store.reset(
        thread_id,
        [
            Message(role="user", content=[Text(text="fresh")]),
            Message(role="assistant", content=[Text(text="ok")]),
        ],
    )
    threads = await store.list_threads(limit=10)
    row = next(t for t in threads if t.thread_id == thread_id)
    assert row.message_count == 2
    assert "ok" in row.preview


@pytest.mark.asyncio
async def test_firestore_load_skips_corrupt_rows(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    thread_id = "thread-corrupt"
    await store.append(thread_id, Message(role="user", content=[Text(text="good")]))
    await store._client.collection(store._collection).add(
        {
            "thread_id": thread_id,
            "role": "user",
            "content": "not-json",
            "created_at": 2,
        }
    )
    await store.append(thread_id, Message(role="user", content=[Text(text="also good")]))

    loaded = await store.load(thread_id)
    texts = [b.text for m in loaded for b in m.content if isinstance(b, Text)]
    assert texts == ["good", "also good"]


@pytest.mark.asyncio
async def test_firestore_concurrent_message_id_is_idempotent(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    message = Message(role="user", content=[Text(text="deliver once")])

    await asyncio.gather(
        *[
            store.append(
                "thread-idempotent",
                message,
                turn_id="turn-1",
                message_id="message-1",
            )
            for _ in range(8)
        ]
    )

    loaded = await store.load("thread-idempotent")
    assert len(loaded) == 1
    threads = await store.list_threads(limit=10)
    row = next(item for item in threads if item.thread_id == "thread-idempotent")
    assert row.message_count == 1
