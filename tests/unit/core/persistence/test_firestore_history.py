"""Firestore history store tests (require a running Firestore emulator)."""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

pytest.importorskip("google.cloud.firestore")

from google.cloud.firestore import AsyncClient  # noqa: E402

import monkeybot.core.persistence.firestore as firestore_mod
from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.firestore import (
    FirestoreHistoryStore,
    _legacy_summary_document_id,
    firestore_summary_doc_id,
)
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
    store = FirestoreHistoryStore(MagicMock(), "prefix", "agent-a")
    doc_id = store._summary_doc_id("weird/thread/id")
    assert "/" not in doc_id
    assert doc_id == store._summary_doc_id("weird/thread/id")


def test_summary_doc_id_differs_by_scope_and_thread_id() -> None:
    a = FirestoreHistoryStore(MagicMock(), "prefix", "agent-a")
    b = FirestoreHistoryStore(MagicMock(), "prefix", "agent-b")
    assert a._summary_doc_id("t1") != b._summary_doc_id("t1")
    assert a._summary_doc_id("t1") != a._summary_doc_id("t2")


def test_legacy_summary_document_id_rejects_illegal_ids() -> None:
    assert _legacy_summary_document_id("thread-1") == "thread-1"
    assert _legacy_summary_document_id("a/b") is None
    assert _legacy_summary_document_id("") is None
    assert _legacy_summary_document_id(".") is None
    assert _legacy_summary_document_id("..") is None
    assert _legacy_summary_document_id("x" * 1501) is None


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
    store = FirestoreHistoryStore(MagicMock(), "prefix", "agent-a")
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
    docs = [
        _FakeDoc(
            {
                "thread_id": f"t{i}",
                "last_message_at": 100 - i,
                "message_count": 1,
                "last_content": "",
            }
        )
        for i in range(5)
    ]
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


@pytest.mark.asyncio
async def test_list_threads_dedupes_legacy_raw_summary() -> None:
    """A raw-id summary and the hashed summary for one thread are one entry.

    The newer document is first, which is what the descending query returns,
    and a page full of duplicates still reaches the next thread.
    """
    docs = [
        _FakeDoc(
            {
                "thread_id": "t1",
                "last_message_at": 5,
                "message_count": 3,
                "last_content": '[{"type":"text","text":"newer"}]',
            }
        ),
        _FakeDoc(
            {
                "thread_id": "t1",
                "last_message_at": 4,
                "message_count": 2,
                "last_content": '[{"type":"text","text":"older"}]',
            }
        ),
        _FakeDoc(
            {
                "thread_id": "t2",
                "last_message_at": 1,
                "message_count": 1,
                "last_content": "",
            }
        ),
    ]
    client = _FakeFirestoreClient(docs)
    store = FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]
    threads = await store.list_threads(limit=2)
    assert [item.thread_id for item in threads] == ["t1", "t2"]
    assert threads[0].message_count == 3
    assert threads[0].preview == "newer"


class _OverlapQuery:
    """Blocks each read until both the newest-row stream and the count have started."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._release = asyncio.Event()

    def where(self, *_args, **_kwargs) -> _OverlapQuery:
        return self

    def order_by(self, *_args, **_kwargs) -> _OverlapQuery:
        return self

    def limit(self, _n: int) -> _OverlapQuery:
        return self

    def count(self) -> _OverlapQuery:
        return self

    async def _arrive(self, name: str) -> None:
        self.events.append(name)
        if len(self.events) >= 2:
            self._release.set()
        await self._release.wait()

    async def stream(self):
        await self._arrive("stream")
        yield _FakeDoc({"content": "[]"})

    async def get(self):
        await self._arrive("count")
        return [[type("Agg", (), {"value": 1})()]]


class _OverlapClient:
    def __init__(self, query: _OverlapQuery) -> None:
        self._query = query

    def collection(self, _name: str) -> _OverlapQuery:
        return self._query


@pytest.mark.asyncio
async def test_last_row_starts_both_reads_before_either_finishes() -> None:
    query = _OverlapQuery()
    store = FirestoreHistoryStore(_OverlapClient(query), "prefix", "agent-a")  # type: ignore[arg-type]
    count, content = await asyncio.wait_for(store.last_row("t"), timeout=1)
    assert sorted(query.events) == ["count", "stream"]
    assert count == 1
    assert content == "[]"


@pytest_asyncio.fixture
async def firestore_history(require_emulator: None):
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    client = AsyncClient(project="monkeybot-test", database="(default)")
    store = FirestoreHistoryStore(client, prefix)
    yield store
    for collection in (
        store._collection,
        store._threads_collection,
        store._archive_collection,
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
        for collection in (
            store_a._collection,
            store_a._threads_collection,
            store_a._archive_collection,
        ):
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


def _count_rpcs(client: AsyncClient) -> dict[str, int]:
    """Count Firestore RPCs on ``client``. Later calls replace nothing already wrapped."""
    counts: dict[str, int] = {}
    api = client._firestore_api
    for name in (
        "run_query",
        "commit",
        "begin_transaction",
        "batch_get_documents",
        "run_aggregation_query",
    ):
        original = getattr(api, name)

        async def wrapped(*args, _name=name, _original=original, **kwargs):
            counts[_name] = counts.get(_name, 0) + 1
            return await _original(*args, **kwargs)

        setattr(api, name, wrapped)
    return counts


def _texts(messages: list[Message]) -> list[str]:
    return [
        block.text for message in messages for block in message.content if isinstance(block, Text)
    ]


@pytest.mark.asyncio
async def test_plain_append_commits_once(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    counts = _count_rpcs(store._client)
    await store.append("thread-plain", Message(role="user", content=[Text(text="hello")]))
    assert counts["commit"] == 1
    assert "run_query" not in counts
    loaded = await store.load("thread-plain")
    assert _texts(loaded) == ["hello"]


@pytest.mark.asyncio
async def test_message_id_append_writes_hashed_summary_only(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    thread_id = "thread-hashed"
    await store.append(
        thread_id,
        Message(role="user", content=[Text(text="once")]),
        turn_id="turn-1",
        message_id="message-hashed",
    )
    client = store._client
    raw = await client.collection(store._threads_collection).document(thread_id).get()
    hashed = (
        await client.collection(store._threads_collection)
        .document(store._summary_doc_id(thread_id))
        .get()
    )
    assert raw.exists is False
    data = hashed.to_dict() or {}
    assert data["thread_id"] == thread_id
    assert data["message_count"] == 1
    assert data["agent_scope"] == store._agent_scope


@pytest.mark.asyncio
async def test_hidden_appends_write_no_summary(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    hidden = "subagent:parent:child"
    message = Message(role="user", content=[Text(text="hidden")])
    await store.append(hidden, message, turn_id="turn-1", message_id="message-hidden")
    await store.append_with_outbox(
        hidden,
        message,
        turn_id="turn-2",
        message_id="message-hidden-outbox",
        outbox={
            "agent_id": "agent",
            "thread_id": hidden,
            "message_id": "message-hidden-outbox",
            "role": "user",
            "content": "hidden",
        },
    )
    found: list[str] = []
    async for doc in store._client.collection(store._threads_collection).stream():
        data = doc.to_dict() or {}
        if data.get("thread_id") == hidden:
            found.append(doc.id)
    assert found == []
    assert len(await store.load(hidden)) == 2


@pytest.mark.asyncio
async def test_outbox_append_reads_both_documents_once(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    counts = _count_rpcs(store._client)
    await store.append_with_outbox(
        "thread-outbox",
        Message(role="user", content=[Text(text="with outbox")]),
        turn_id="turn-1",
        message_id="message-outbox",
        outbox={
            "agent_id": "agent",
            "thread_id": "thread-outbox",
            "message_id": "message-outbox",
            "role": "user",
            "content": "with outbox",
        },
    )
    assert counts["batch_get_documents"] == 1
    assert counts["run_query"] == 1
    assert counts["commit"] == 1
    assert _texts(await store.load("thread-outbox")) == ["with outbox"]


@pytest.mark.asyncio
async def test_reset_archiving_reads_once_and_commits_once(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    thread_id = "thread-compact"
    for i in range(4):
        await store.append(thread_id, Message(role="user", content=[Text(text=f"m{i}")]))
    kept = await store.load(thread_id)
    counts = _count_rpcs(store._client)
    archived = await store.reset_archiving(
        thread_id, kept[:1], reason="compaction", summary_row_id="summary-1"
    )
    assert archived == 3
    assert counts["run_query"] == 1
    assert counts["commit"] == 1
    assert _texts(await store.load(thread_id)) == ["m0"]
    archive = await store.load_archive(thread_id)
    assert _texts([row.message for row in archive]) == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_over_limit_rewrite_still_archives_before_deleting(
    firestore_history, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(firestore_mod, "_FIRESTORE_BATCH_LIMIT", 2)
    store: FirestoreHistoryStore = firestore_history
    thread_id = "thread-chunked"
    for i in range(4):
        await store.append(thread_id, Message(role="user", content=[Text(text=f"m{i}")]))
    kept = await store.load(thread_id)
    counts = _count_rpcs(store._client)
    archived = await store.reset_archiving(
        thread_id, kept[:1], reason="compaction", summary_row_id=None
    )
    assert archived == 3
    assert counts["run_query"] == 1
    assert counts["commit"] > 1
    assert _texts(await store.load(thread_id)) == ["m0"]
    assert len(await store.load_archive(thread_id)) == 3


@pytest.mark.asyncio
async def test_rewrite_keeps_a_row_absent_from_the_snapshot(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    thread_id = "thread-race"
    for i in range(3):
        await store.append(
            thread_id,
            Message(role="user", content=[Text(text=f"m{i}")]),
            turn_id="turn-1",
            message_id=f"race-{i}",
        )
    kept = await store.load(thread_id)
    real = store._stored_docs

    async def stale(thread: str):
        docs = await real(thread)
        await FirestoreHistoryStore.append(
            store,
            thread,
            Message(role="user", content=[Text(text="late")]),
            turn_id="turn-1",
            message_id="race-late",
        )
        return docs

    store._stored_docs = stale  # type: ignore[method-assign]
    await store.reset(thread_id, kept[:1])
    assert _texts(await store.load(thread_id)) == ["m0", "late"]


@pytest.mark.asyncio
async def test_purge_removes_same_scope_raw_summary_only(firestore_history) -> None:
    store: FirestoreHistoryStore = firestore_history
    client = store._client
    threads = client.collection(store._threads_collection)
    await threads.document("mine").set(
        {
            "thread_id": "mine",
            "agent_scope": store._agent_scope,
            "last_message_at": 1,
            "message_count": 9,
            "last_content": "stale",
        }
    )
    await threads.document("shared").set(
        {
            "thread_id": "shared",
            "agent_scope": "other-agent",
            "last_message_at": 1,
            "message_count": 4,
            "last_content": "keep",
        }
    )
    await store.append("mine", Message(role="user", content=[Text(text="live")]))
    await store.append("shared", Message(role="user", content=[Text(text="live")]))
    await store.reset("mine", [])
    await store.reset("shared", [])
    assert (await threads.document("mine").get()).exists is False
    assert (await threads.document(store._summary_doc_id("mine")).get()).exists is False
    shared = await threads.document("shared").get()
    assert shared.exists is True
    assert (shared.to_dict() or {})["agent_scope"] == "other-agent"
    assert (await threads.document(store._summary_doc_id("shared")).get()).exists is False
