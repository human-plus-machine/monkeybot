"""Tests for SQLite DB helpers and conversation history."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import (
    SCHEMA_DDLS,
    apply_schema,
    open_connection,
    sqlite_path_from_db_url,
)
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse

# Legacy ChatMessage + tool_* columns removed in story-2-persistence; see design 1B §7.8.


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def history_db():
    conn = await open_connection("sqlite:///:memory:")
    await apply_schema(conn)
    history = SQLiteHistoryStore(conn)
    yield conn, history
    await conn.close()


def test_sqlite_path_from_db_url_memory() -> None:
    assert sqlite_path_from_db_url("sqlite:///:memory:") == ":memory:"


def test_sqlite_path_from_db_url_file_four_slashes() -> None:
    assert sqlite_path_from_db_url("sqlite:////tmp/t.db") == "/tmp/t.db"


def test_sqlite_path_from_db_url_rejects_non_sqlite() -> None:
    with pytest.raises(ValueError):
        sqlite_path_from_db_url("postgres://localhost/x")


@pytest.mark.asyncio
async def test_open_connection_enables_wal(tmp_path: Path) -> None:
    db_file = (tmp_path / "wal.db").resolve()
    conn = await open_connection("sqlite:///" + db_file.as_posix())
    try:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert str(row[0]).lower() == "wal"
    finally:
        await conn.close()


def test_schema_ddls_cover_all_tables() -> None:
    joined = "\n".join(SCHEMA_DDLS)
    assert "conversation_history" in joined
    assert "subagent_runs" in joined
    assert "turn_usage" in joined


@pytest.mark.asyncio
async def test_apply_schema_creates_only_expected_history_columns() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        await apply_schema(conn)
        cursor = await conn.execute("PRAGMA table_info(conversation_history)")
        rows = await cursor.fetchall()
        await cursor.close()
        names = {str(r[1]) for r in rows}
        assert names == {"id", "thread_id", "role", "content", "created_at"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_schema_raises_when_legacy_tool_columns_present() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        await conn.execute("DROP TABLE IF EXISTS conversation_history")
        await conn.execute(
            """CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_call_id TEXT,
                tool_name TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        await conn.commit()
        with pytest.raises(RuntimeError) as excinfo:
            await apply_schema(conn)
        msg = str(excinfo.value)
        assert "Legacy conversation_history schema detected" in msg
        assert "rm -f playground/agent/workspace/data/monkeybot.db" in msg
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_schema_succeeds_after_wipe_simulated(tmp_path: Path) -> None:
    db_file = tmp_path / "wipe_me.db"
    conn = await open_connection(f"sqlite:///{db_file.as_posix()}")
    try:
        await conn.execute("DROP TABLE IF EXISTS conversation_history")
        await conn.execute(
            """CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_call_id TEXT,
                tool_name TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        await conn.commit()
        with pytest.raises(RuntimeError, match="Legacy conversation_history schema detected"):
            await apply_schema(conn)
    finally:
        await conn.close()

    db_file.unlink(missing_ok=True)
    for suffix in "-shm", "-wal":
        p = Path(str(db_file) + suffix)
        p.unlink(missing_ok=True)

    conn2 = await open_connection(f"sqlite:///{db_file.as_posix()}")
    try:
        await apply_schema(conn2)
        cursor = await conn2.execute("PRAGMA table_info(conversation_history)")
        rows = await cursor.fetchall()
        await cursor.close()
        names = {str(r[1]) for r in rows}
        assert names == {"id", "thread_id", "role", "content", "created_at"}
    finally:
        await conn2.close()


@pytest.mark.asyncio
async def test_history_text_message_roundtrip(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-text-rt"
    original = Message.text("user", "hi")
    await history.append(thread_id, original)
    loaded = await history.load(thread_id)
    assert loaded == [original]


@pytest.mark.asyncio
async def test_history_tool_response_roundtrip(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-tool-resp-rt"
    original = Message(
        role="user",
        content=[ToolResponse(id="x", tool_name="echo", result=[Text(text="ok")])],
    )
    await history.append(thread_id, original)
    loaded = await history.load(thread_id)
    assert loaded == [original]


@pytest.mark.asyncio
async def test_history_mixed_blocks_roundtrip(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-mixed-rt"
    original = Message(
        role="assistant",
        content=[
            Text(text="a"),
            ToolRequest(id="c1", name="echo", args={"x": 1}),
        ],
    )
    await history.append(thread_id, original)
    loaded = await history.load(thread_id)
    assert loaded == [original]


@pytest.mark.asyncio
async def test_history_empty_content_roundtrip(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-empty-content"
    original = Message(role="assistant", content=[])
    await history.append(thread_id, original)
    loaded = await history.load(thread_id)
    assert loaded == [original]


@pytest.mark.asyncio
async def test_history_append_rejects_tool_role(history_db) -> None:
    _conn, history = history_db

    class _ToolRole:
        role = "tool"
        content: list[Any] = []

    with pytest.raises(ValueError):
        await history.append("t1", cast(Message, _ToolRole()))


@pytest.mark.asyncio
async def test_history_rejects_invalid_role(history_db) -> None:
    _conn, history = history_db

    class _BadRole:
        role = "nope"
        content: list[Any] = []

    with pytest.raises(ValueError):
        await history.append("t1", cast(Message, _BadRole()))


@pytest.mark.asyncio
async def test_history_load_malformed_json_logs_and_raises(history_db, caplog: pytest.LogCaptureFixture) -> None:
    conn, history = history_db
    thread_id = "t-bad-json"
    await conn.execute(
        """
        INSERT INTO conversation_history(thread_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (thread_id, "user", "not-json", 1),
    )
    await conn.commit()
    with caplog.at_level(logging.ERROR, logger="monkeybot.core.persistence.history"):
        with pytest.raises(ValueError, match=r"history row \d+ unparseable"):
            await history.load(thread_id)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "not-json" not in joined


@pytest.mark.asyncio
async def test_history_content_sqlite_json_extract(history_db) -> None:
    conn, history = history_db
    thread_id = "t-json-extract"
    msg = Message(
        role="assistant",
        content=[
            Text(text="x"),
            ToolRequest(id="r1", name="echo", args={}),
        ],
    )
    await history.append(thread_id, msg)
    cursor = await conn.execute(
        "SELECT json_extract(content, '$[0].type') FROM conversation_history WHERE thread_id = ?",
        (thread_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == "text"


@pytest.mark.asyncio
async def test_history_append_load_ordering_limit_clear_reset(history_db) -> None:
    conn, history = history_db
    thread_order = "t-ordering"
    await history.append(thread_order, Message.text("user", "U1"))
    await history.append(thread_order, Message.text("assistant", "A1"))
    await history.append(thread_order, Message.text("user", "U2"))
    loaded = await history.load(thread_order)
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    flat = [b.text for m in loaded for b in m.content if isinstance(b, Text)]
    assert flat == ["U1", "A1", "U2"]

    thread_limit = "t-limit"
    for i in range(150):
        await history.append(thread_limit, Message.text("user", f"m{i}"))
    limited = await history.load(thread_limit, limit=100)
    assert len(limited) == 100
    lim_texts = [b.text for m in limited for b in m.content if isinstance(b, Text)]
    assert lim_texts[0] == "m50"
    assert lim_texts[-1] == "m149"

    thread_reset = "t-reset"
    for i in range(5):
        await history.append(thread_reset, Message.text("user", f"m{i}"))
    await history.reset(
        thread_reset,
        [
            Message.text("user", "a"),
            Message.text("assistant", "b"),
        ],
    )
    after_reset = await history.load(thread_reset)
    assert len(after_reset) == 2
    assert [b.text for m in after_reset for b in m.content if isinstance(b, Text)] == ["a", "b"]

    thread_empty_reset = "t-empty-reset"
    await history.append(thread_empty_reset, Message.text("user", "x"))
    await history.reset(thread_empty_reset, [])
    assert await history.load(thread_empty_reset) == []

    await history.append("cle1", Message.text("user", "a"))
    await history.append("cle2", Message.text("user", "b"))
    await history.clear("cle1")
    assert await history.load("cle1") == []
    t2 = await history.load("cle2")
    assert len(t2) == 1
    assert t2[0] == Message.text("user", "b")


def test_sqlite_persistence_source_has_no_google_cloud() -> None:
    root = _repo_root()
    paths = [
        root / "src/monkeybot/core/persistence/sqlite.py",
        root / "src/monkeybot/core/persistence/history.py",
        root / "src/monkeybot/core/persistence/durable_runs.py",
        root / "src/monkeybot/core/llm/usage.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "google.cloud" not in text
        assert "firestore" not in text


def test_importing_sqlite_does_not_import_google_cloud() -> None:
    """Load ``sqlite.py`` in isolation so package ``__init__`` side effects are avoided."""
    root = _repo_root()
    sqlite_path = root / "src/monkeybot/core/persistence/sqlite.py"
    before = {k for k in sys.modules if k.startswith("google.cloud")}
    module_name = "_monkeybot_sqlite_standalone_test"
    spec = importlib.util.spec_from_file_location(module_name, sqlite_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    after = {k for k in sys.modules if k.startswith("google.cloud")}
    assert after == before
