"""Stable ``Message.row_id`` across stores and every history rewrite path."""

from __future__ import annotations

import json
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


async def _stored(store: SQLiteHistoryStore, thread_id: str = "t") -> list[tuple[Any, ...]]:
    cursor = await store._conn.execute(
        """
        SELECT created_at, turn_id, message_id, row_id, content
        FROM conversation_history
        WHERE thread_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (thread_id,),
    )
    rows = [tuple(row) for row in await cursor.fetchall()]
    await cursor.close()
    return rows


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
    loaded = await store.load("t")
    original = {m.row_id for m in loaded}
    summarized = await _summarize_history(
        "t",
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
async def test_compaction_on_raw_rows_keeps_rows_transform_context_would_merge(store) -> None:
    """A UI-only row between same-role rows is merged by transform_context; raw compaction keeps all."""
    for i in range(12):
        await store.append("t", _text("user" if i % 2 == 0 else "assistant", "x" * 400 + str(i)))
    await store.append(
        "t",
        Message(
            role="user",
            content=[SystemNotification(notification_type="verifierVerdict", msg="ok")],
        ),
    )
    await store.append("t", _text("assistant", "tail-a"))
    await store.append("t", _text("user", "tail-b"))
    await store.append("t", _text("assistant", "tail-c"))
    raw = await store.load("t")
    assert len(transform_context(raw)) < len(raw)
    ids_before = {m.row_id for m in raw}
    assert await _summarize_history(
        "t",
        store,
        _SummaryProvider(),  # type: ignore[arg-type]
        "m",
        window_tokens=100,
    )
    rows = await store.load("t")
    assert any(
        isinstance(b, SystemNotification) and b.notification_type == "verifierVerdict"
        for m in rows
        for b in m.content
    )
    tail_ids = {m.row_id for m in raw[-3:]}
    assert tail_ids <= {m.row_id for m in rows}
    assert {m.row_id for m in rows} - ids_before  # summary row is new


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


def _payload(text: str) -> str:
    return json.dumps([{"type": "text", "text": text}], separators=(",", ":"), ensure_ascii=False)


@pytest.mark.asyncio
async def test_reset_keeps_created_at_turn_id_and_message_id(store: SQLiteHistoryStore) -> None:
    specs = (("head", "h", "turn-a", "m-head", 1000), ("middle", "m", "turn-a", "m-mid", 2000))
    for text, row_id, turn_id, message_id, _stamp in specs:
        await store.append(
            "t",
            Message(role="user", content=[Text(text=text)], row_id=row_id),
            turn_id=turn_id,
            message_id=message_id,
        )
    await store.append(
        "t",
        Message(role="assistant", content=[Text(text="tail")], row_id="tail"),
        turn_id="turn-b",
        message_id="m-tail",
    )
    for _text, _row_id, _turn_id, message_id, stamp in (
        *specs,
        ("tail", "tail", "turn-b", "m-tail", 3000),
    ):
        await store._conn.execute(
            "UPDATE conversation_history SET created_at = ? WHERE message_id = ?",
            (stamp, message_id),
        )
    await store._conn.commit()

    loaded = await store.load("t")
    await store.reset(
        "t",
        [loaded[0], Message(role="assistant", content=[Text(text="summary")]), loaded[2]],
    )
    rows = await _stored(store)
    assert [row[0] for row in rows] == [1000, 1000, 3000]
    assert [row[1] for row in rows] == ["turn-a", None, "turn-b"]
    assert [row[2] for row in rows] == ["m-head", None, "m-tail"]
    assert rows[0][3] == "h" and rows[2][3] == "tail"
    assert rows[1][3] not in {None, "h", "tail"}
    await store.append(
        "t",
        Message(role="user", content=[Text(text="again")]),
        turn_id="turn-retry",
        message_id="m-head",
    )
    assert len(await store.load("t")) == 3
    listed = await store.list_threads()
    assert listed[0].thread_id == "t"
    assert listed[0].last_message_at == 3000


@pytest.mark.asyncio
async def test_reset_matches_legacy_rows_and_duplicate_row_ids_in_order(
    store: SQLiteHistoryStore,
) -> None:
    await store.append(
        "t",
        Message(role="user", content=[Text(text="first")], row_id="dup"),
        turn_id="t",
        message_id="m1",
    )
    await store.append(
        "t",
        Message(role="user", content=[Text(text="second")], row_id="dup"),
        turn_id="t",
        message_id="m2",
    )
    await store.append(
        "t",
        Message(role="assistant", content=[Text(text="old")]),
        turn_id="t0",
        message_id="m-old",
    )
    await store._conn.execute(
        "UPDATE conversation_history SET created_at = 1000 WHERE message_id = 'm1'"
    )
    await store._conn.execute(
        "UPDATE conversation_history SET created_at = 2000 WHERE message_id = 'm2'"
    )
    await store._conn.execute(
        "UPDATE conversation_history SET created_at = 3000, row_id = NULL WHERE message_id = 'm-old'"
    )
    await store._conn.commit()
    loaded = await store.load("t")
    assert loaded[2].row_id is not None and loaded[2].row_id.startswith(LEGACY_ROW_ID_PREFIX)
    # Both survivors share a row_id, so they pair in stored order rather than by content.
    await store.reset("t", [loaded[1], loaded[0], loaded[2]])
    rows = await _stored(store)
    assert [json.loads(str(row[4]))[0]["text"] for row in rows] == ["second", "first", "old"]
    assert [row[2] for row in rows] == ["m1", "m2", "m-old"]
    assert [row[0] for row in rows] == [1000, 2000, 3000]
    assert rows[2][3] == loaded[2].row_id


@pytest.mark.asyncio
async def test_reset_without_memory_columns_keeps_created_at() -> None:
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
        for text, stamp in (("a", 1000), ("b", 2000)):
            await conn.execute(
                "INSERT INTO conversation_history(thread_id, role, content, created_at) "
                "VALUES ('t', 'user', ?, ?)",
                (_payload(text), stamp),
            )
        await conn.commit()
        bare = SQLiteHistoryStore(conn)
        loaded = await bare.load("t")
        await bare.reset(
            "t",
            [loaded[0], Message(role="assistant", content=[Text(text="new")]), loaded[1]],
        )
        cursor = await conn.execute(
            "SELECT created_at, content FROM conversation_history "
            "WHERE thread_id = 't' ORDER BY created_at, id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert [row[0] for row in rows] == [1000, 1000, 2000]
        assert "new" in str(rows[1][1])
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_postgres_reset_rewrites_with_stored_metadata() -> None:
    conn = _PgConn(
        [
            {
                "id": 4,
                "row_id": "drop",
                "created_at": 3000,
                "turn_id": "t-old",
                "message_id": "m-drop",
            },
            {
                "id": 2,
                "row_id": "keep",
                "created_at": 1000,
                "turn_id": "t-old",
                "message_id": "m-keep",
            },
        ]
    )
    await _pg_store(conn).reset(
        "t",
        [
            Message(role="user", content=[Text(text="kept")], row_id="keep"),
            Message(role="assistant", content=[Text(text="summary")]),
        ],
    )
    inserts = [args for query, args in conn.executed if "INSERT" in query]
    assert len(inserts) == 2
    assert inserts[0][3:] == (1000, "agent-a", "t-old", "m-keep", "keep")
    assert inserts[1][3] == 1000
    assert inserts[1][5] is None and inserts[1][6] is None
    assert inserts[1][-1] not in ("", None, "keep")


class _ResetRef:
    def __init__(self, doc_id: str) -> None:
        self.id = doc_id

    async def set(self, data: dict[str, Any], merge: bool = False) -> None:
        del data, merge

    async def delete(self) -> None:
        return None


class _ResetDoc:
    def __init__(self, client: _ResetClient, doc_id: str, data: dict[str, Any]) -> None:
        self.id = doc_id
        self._data = data
        self.reference = _ResetRef(doc_id)
        self._client = client

    def to_dict(self) -> dict[str, Any]:
        return self._data


class _ResetQuery:
    def __init__(self, docs: list[_ResetDoc]) -> None:
        self._docs = docs

    def where(self, *_args: Any, **_kwargs: Any) -> _ResetQuery:
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> _ResetQuery:
        return self

    async def stream(self) -> AsyncIterator[_ResetDoc]:
        for doc in self._docs:
            yield doc


class _ResetCollection:
    def __init__(self, client: _ResetClient, docs: list[_ResetDoc]) -> None:
        self._client = client
        self._docs = docs
        self._auto = 0

    def where(self, *_args: Any, **_kwargs: Any) -> _ResetQuery:
        return _ResetQuery(self._docs)

    def document(self, doc_id: str | None = None) -> _ResetRef:
        if doc_id is None:
            self._auto += 1
            doc_id = f"auto-{self._auto}"
        ref = _ResetRef(doc_id)
        ref.set = self._record_set(doc_id)  # type: ignore[method-assign]
        ref.delete = self._record_delete(doc_id)  # type: ignore[method-assign]
        return ref

    def _record_set(self, doc_id: str) -> Any:
        async def _set(data: dict[str, Any], merge: bool = False) -> None:
            self._client.direct.append(("set", doc_id, data, merge))

        return _set

    def _record_delete(self, doc_id: str) -> Any:
        async def _delete() -> None:
            self._client.direct.append(("delete", doc_id, None, False))

        return _delete


class _ResetBatch:
    def __init__(self, client: _ResetClient) -> None:
        self._client = client
        self._ops: list[tuple[str, str, dict[str, Any] | None]] = []

    def set(self, ref: _ResetRef, data: dict[str, Any]) -> None:
        self._ops.append(("set", ref.id, data))

    def update(self, ref: _ResetRef, data: dict[str, Any]) -> None:
        self._ops.append(("update", ref.id, data))

    def delete(self, ref: _ResetRef) -> None:
        self._ops.append(("delete", ref.id, None))

    async def commit(self) -> None:
        self._client.commits.append(self._ops)


class _ResetClient:
    def __init__(self, docs: list[tuple[str, dict[str, Any]]]) -> None:
        self.commits: list[list[tuple[str, str, dict[str, Any] | None]]] = []
        self.direct: list[tuple[str, str, dict[str, Any] | None, bool]] = []
        self._docs = [_ResetDoc(self, doc_id, data) for doc_id, data in docs]
        self._history = _ResetCollection(self, self._docs)
        self._threads = _ResetCollection(self, [])

    def collection(self, name: str) -> _ResetCollection:
        if name.endswith("threads"):
            return self._threads
        return self._history

    def batch(self) -> _ResetBatch:
        return _ResetBatch(self)


def _reset_store(client: _ResetClient) -> Any:
    pytest.importorskip("google.cloud.firestore")
    from monkeybot.core.persistence.firestore import FirestoreHistoryStore

    return FirestoreHistoryStore(client, "prefix", "agent-a")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_firestore_reset_writes_nothing_for_an_unchanged_row() -> None:
    message = Message(role="user", content=[Text(text="kept")], row_id="keep")
    client = _ResetClient(
        [
            (
                "doc-1",
                {
                    "role": "user",
                    "content": _payload("kept"),
                    "created_at": 1000,
                    "turn_id": "turn-old",
                    "message_id": "m-old",
                    "row_id": "keep",
                },
            )
        ]
    )
    await _reset_store(client).reset("t", [message])
    assert client.commits == []
    assert client.direct[0][0] == "set"
    assert client.direct[0][2] is not None
    assert client.direct[0][2]["last_message_at"] == 1000
    assert client.direct[0][2]["message_count"] == 1


@pytest.mark.asyncio
async def test_firestore_reset_commits_sets_before_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("monkeybot.core.persistence.firestore._FIRESTORE_BATCH_LIMIT", 2)
    client = _ResetClient(
        [
            (
                "keep-doc",
                {
                    "role": "user",
                    "content": _payload("stale"),
                    "created_at": 1000,
                    "turn_id": "turn-old",
                    "message_id": "m-keep",
                    "row_id": "keep",
                },
            ),
            (
                "drop-doc",
                {
                    "role": "assistant",
                    "content": _payload("gone"),
                    "created_at": 3000,
                    "turn_id": "turn-old",
                    "message_id": "m-drop",
                    "row_id": "drop",
                },
            ),
        ]
    )
    await _reset_store(client).reset(
        "t",
        [
            Message(role="user", content=[Text(text="kept")], row_id="keep"),
            Message(role="assistant", content=[Text(text="summary")]),
            Message(role="user", content=[Text(text="extra")]),
        ],
    )
    assert [len(commit) for commit in client.commits] == [2, 1, 1]
    writes = [op for commit in client.commits[:-1] for op in commit]
    assert {op[0] for op in writes} <= {"set", "update"}
    assert client.commits[-1][0][0] == "delete"
    assert client.commits[-1][0][1] == "drop-doc"
    update = next(op for op in writes if op[0] == "update")
    assert update[2] is not None
    assert "turn_id" not in update[2] and "message_id" not in update[2]
    assert "kept" in str(update[2]["content"])
    sets = [op[2] for op in writes if op[0] == "set" and op[2] is not None]
    assert [item["created_at"] for item in sets] == [1001, 1002]
    assert all(item["message_id"] is None and item["turn_id"] is None for item in sets)
