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
from monkeybot.core.persistence.firestore import FirestoreHistoryStore
from monkeybot.core.types.content_blocks import Text


def _emulator_host() -> str | None:
    return os.environ.get("FIRESTORE_EMULATOR_HOST")


@pytest.fixture
def require_emulator() -> None:
    if not _emulator_host():
        pytest.skip("FIRESTORE_EMULATOR_HOST not set")


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
