"""Stable ``Message.row_id`` across stores and every history rewrite path."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.messages.transform_context import transform_context
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.row_ids import LEGACY_ROW_ID_PREFIX
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.runtime.history_compaction import (
    _summarize_history,
    truncate_history_preserving_pins,
)
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    SystemNotification,
    Text,
    ToolRequest,
    ToolResponse,
)


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[Text(text=text)])  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def store():
    conn = await open_connection("sqlite:///:memory:")
    await apply_schema(conn)
    yield SQLiteHistoryStore(conn)
    await conn.close()


async def _ids(store: SQLiteHistoryStore, thread_id: str = "t") -> list[str | None]:
    return [m.row_id for m in await store.load(thread_id)]


@pytest.mark.asyncio
async def test_append_assigns_unique_ids_that_load_returns_unchanged(store) -> None:
    for i in range(3):
        await store.append("t", _text("user" if i % 2 == 0 else "assistant", f"m{i}"))
    first = await _ids(store)
    assert all(first) and len(set(first)) == 3
    assert await _ids(store) == first
    assert [m.row_id for m in await store.load("t", limit=2)] == first[1:]


@pytest.mark.asyncio
async def test_append_keeps_an_existing_row_id(store) -> None:
    await store.append("t", Message(role="user", content=[Text(text="hi")], row_id="r-1"))
    await store.append("other", (await store.load("t"))[0])
    assert await _ids(store) == ["r-1"]
    assert await _ids(store, "other") == ["r-1"]


@pytest.mark.asyncio
async def test_reset_preserves_ids_and_assigns_new_rows_fresh_ids(store) -> None:
    await store.append("t", _text("user", "a"))
    await store.append("t", _text("assistant", "b"))
    loaded = await store.load("t")
    await store.reset("t", [*loaded, _text("user", "c")])
    ids = await _ids(store)
    assert ids[:2] == [m.row_id for m in loaded]
    assert ids[2] and ids[2] not in ids[:2]


@pytest.mark.asyncio
async def test_row_id_does_not_affect_message_equality() -> None:
    a = Message(role="user", content=[Text(text="x")], row_id="a")
    b = Message(role="user", content=[Text(text="x")], row_id="b")
    assert a == b


@pytest.mark.asyncio
async def test_legacy_rows_get_stable_ids_until_rewritten(store) -> None:
    await store.append("t", _text("user", "old"))
    await store._conn.execute("UPDATE conversation_history SET row_id = NULL")
    await store._conn.commit()
    legacy = await _ids(store)
    assert legacy[0] is not None and legacy[0].startswith(LEGACY_ROW_ID_PREFIX)
    assert await _ids(store) == legacy
    await store.reset("t", await store.load("t"))
    assert await _ids(store) == legacy


@pytest.mark.asyncio
async def test_upgrades_a_database_without_the_row_id_column() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        await conn.execute(
            """CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                agent_scope TEXT NOT NULL DEFAULT '',
                turn_id TEXT,
                message_id TEXT
            )"""
        )
        await conn.execute(
            "INSERT INTO conversation_history(thread_id, role, content, created_at) "
            """VALUES ('t', 'user', '[{"type":"text","text":"old"}]', 1)"""
        )
        await conn.commit()
        await apply_schema(conn)
        upgraded = SQLiteHistoryStore(conn)
        await upgraded.append("t", _text("assistant", "new"))
        ids = [m.row_id for m in await upgraded.load("t")]
        assert ids[0] == f"{LEGACY_ROW_ID_PREFIX}1"
        assert ids[1] and not ids[1].startswith(LEGACY_ROW_ID_PREFIX)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_store_without_row_id_column_still_reads_and_writes() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        await conn.execute(
            """CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                agent_scope TEXT NOT NULL DEFAULT ''
            )"""
        )
        await conn.commit()
        legacy = SQLiteHistoryStore(conn)
        await legacy.append("t", _text("user", "hi"))
        assert [m.row_id for m in await legacy.load("t")] == [f"{LEGACY_ROW_ID_PREFIX}1"]
    finally:
        await conn.close()


def test_transform_context_keeps_row_ids_on_rebuilt_messages() -> None:
    rows = [
        Message(role="user", content=[Text(text="q")], row_id="u1"),
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="t", args={})],
            row_id="a1",
        ),
        Message(
            role="user",
            content=[
                ToolResponse(id="c1", tool_name="t", result=[Text(text="ok")]),
                SystemNotification(notification_type="inlineMessage", msg="ui only"),
            ],
            row_id="u2",
        ),
        Message(role="assistant", content=[Text(text="first")], row_id="a2"),
        Message(
            role="user",
            content=[SystemNotification(notification_type="inlineMessage", msg="ui only")],
            row_id="u3",
        ),
        Message(role="assistant", content=[Text(text="second")], row_id="a3"),
    ]
    out = transform_context(rows)
    assert [m.row_id for m in out] == ["u1", "a1", "u2", "a2"]
    assert [b.text for b in out[-1].content if isinstance(b, Text)] == ["first", "second"]


def test_tool_integrity_repair_keeps_the_user_row_id() -> None:
    rows = [
        Message(
            role="assistant",
            content=[
                ToolRequest(id="c1", name="t", args={}),
                ToolRequest(id="c2", name="t", args={}),
            ],
            row_id="a1",
        ),
        Message(
            role="user",
            content=[ToolResponse(id="c1", tool_name="t", result=[Text(text="ok")])],
            row_id="u1",
        ),
    ]
    out = transform_context(rows)
    assert [m.row_id for m in out] == ["a1", "u1"]
    assert len(out[1].content) == 2


@pytest.mark.asyncio
async def test_freeze_rewrite_preserves_row_ids(store) -> None:
    await store.append(
        "t",
        Message(
            role="user",
            content=[
                Text(text="look"),
                AttachmentRef(attachment_id="att1", mime_type="image/png", metadata={}),
            ],
        ),
    )
    await store.append("t", _text("assistant", "a cat"))
    before = await _ids(store)
    events = await freeze_attachments_in_history(
        thread_id="t", history=store, catalog=None, last_assistant_text="a cat"
    )
    assert events
    rows = await store.load("t")
    assert not any(isinstance(b, AttachmentRef) for b in rows[0].content)
    assert [m.row_id for m in rows] == before


class _SummaryProvider:
    async def stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        yield TextDelta(text="summary")
        yield Done()


@pytest.mark.asyncio
async def test_compaction_keeps_head_and_tail_ids_and_adds_a_summary_row(store) -> None:
    for i in range(20):
        await store.append("t", _text("user" if i % 2 == 0 else "assistant", "x" * 400 + str(i)))
    loaded = transform_context(await store.load("t"))
    original = {m.row_id for m in loaded}
    summarized = await _summarize_history(
        "t",
        loaded,
        store,
        _SummaryProvider(),
        "m",
        window_tokens=100,  # type: ignore[arg-type]
    )
    assert summarized > 0
    rows = await store.load("t")
    kept = [m.row_id for m in rows if m.row_id in original]
    new = [m for m in rows if m.row_id not in original]
    assert len(kept) == len(rows) - 1
    assert kept == [m.row_id for m in loaded if m.row_id in set(kept)]
    assert len(new) == 1 and new[0].row_id
    assert any(isinstance(b, Text) and "summary" in b.text for b in new[0].content)


@pytest.mark.asyncio
async def test_truncate_fallback_preserves_kept_row_ids(store) -> None:
    for i in range(10):
        await store.append("t", _text("user" if i % 2 == 0 else "assistant", f"m{i}"))
    loaded = await store.load("t")
    truncated = truncate_history_preserving_pins(loaded, max_rows=4)
    await store.reset("t", truncated)
    assert await _ids(store) == [m.row_id for m in truncated]


# ---------------------------------------------------------------------------
# Postgres and Firestore: fakes that check what the store writes and reads.
# ---------------------------------------------------------------------------


class _PgConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        return self.rows

    async def fetchval(self, _query: str, *_args: Any) -> None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"

    def transaction(self) -> _PgConn:
        return self

    async def __aenter__(self) -> _PgConn:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _PgPool:
    def __init__(self, conn: _PgConn) -> None:
        self._conn = conn

    def acquire(self) -> _PgConn:
        return self._conn


def _pg_store(conn: _PgConn) -> Any:
    pytest.importorskip("asyncpg")
    from monkeybot.core.persistence.postgres import PostgresHistoryStore

    return PostgresHistoryStore(_PgPool(conn), "agent-a")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_postgres_writes_existing_or_new_row_id() -> None:
    conn = _PgConn()
    store = _pg_store(conn)
    await store.reset("t", [Message(role="user", content=[Text(text="a")], row_id="keep")])
    await store.append("t", _text("assistant", "b"))
    inserts = [args for query, args in conn.executed if "INSERT" in query]
    assert all("row_id" in q for q, _ in conn.executed if "INSERT" in q)
    assert inserts[0][-1] == "keep"
    assert isinstance(inserts[1][-1], str) and inserts[1][-1] not in ("", "keep")


@pytest.mark.asyncio
async def test_postgres_load_returns_stored_or_legacy_row_id() -> None:
    content = '[{"type":"text","text":"x"}]'
    conn = _PgConn(
        [
            {"id": 7, "role": "user", "content": content, "row_id": None},
            {"id": 8, "role": "assistant", "content": content, "row_id": "r8"},
        ]
    )
    rows = await _pg_store(conn).load("t")
    assert [m.row_id for m in rows] == [f"{LEGACY_ROW_ID_PREFIX}7", "r8"]


class _FsDoc:
    def __init__(self, doc_id: str, data: dict[str, Any]) -> None:
        self.id = doc_id
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return self._data


class _FsQuery:
    def __init__(self, docs: list[_FsDoc]) -> None:
        self._docs = docs

    def where(self, *_args: Any, **_kwargs: Any) -> _FsQuery:
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> _FsQuery:
        return self

    def limit(self, n: int) -> _FsQuery:
        return _FsQuery(self._docs[:n])

    async def stream(self) -> AsyncIterator[_FsDoc]:
        for doc in self._docs:
            yield doc


class _FsCollection(_FsQuery):
    def __init__(self, docs: list[_FsDoc], added: list[dict[str, Any]]) -> None:
        super().__init__(docs)
        self._added = added

    async def add(self, data: dict[str, Any]) -> None:
        self._added.append(data)

    def document(self, _doc_id: str) -> Any:
        class _Ref:
            async def set(self, *_a: Any, **_k: Any) -> None:
                return None

        return _Ref()


class _FsClient:
    def __init__(self, docs: list[_FsDoc]) -> None:
        self.docs = docs
        self.added: list[dict[str, Any]] = []

    def collection(self, _name: str) -> _FsCollection:
        return _FsCollection(self.docs, self.added)


def _fs_store(client: _FsClient) -> Any:
    pytest.importorskip("google.cloud.firestore")
    from monkeybot.core.persistence.firestore import FirestoreHistoryStore

    return FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_firestore_append_writes_row_id() -> None:
    client = _FsClient([])
    store = _fs_store(client)
    await store.append("t", Message(role="user", content=[Text(text="a")], row_id="keep"))
    await store.append("t", _text("assistant", "b"))
    assert client.added[0]["row_id"] == "keep"
    assert client.added[1]["row_id"] not in ("", None, "keep")


@pytest.mark.asyncio
async def test_firestore_load_returns_stored_or_legacy_row_id() -> None:
    content = '[{"type":"text","text":"x"}]'
    client = _FsClient(
        [
            _FsDoc("doc-a", {"role": "user", "content": content}),
            _FsDoc("doc-b", {"role": "assistant", "content": content, "row_id": "rb"}),
        ]
    )
    rows = await _fs_store(client).load("t")
    assert [m.row_id for m in rows] == [f"{LEGACY_ROW_ID_PREFIX}doc-a", "rb"]
