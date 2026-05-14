"""Tests for SQLite DB helpers and conversation history."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from monkeybot.core.db import SCHEMA_DDLS, apply_schema, open_connection, sqlite_path_from_db_url
from monkeybot.core.history import ChatMessage, ConversationHistory


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


async def _apply_schema(conn) -> None:
    await apply_schema(conn)


@pytest_asyncio.fixture
async def history_db():
    conn = await open_connection("sqlite:///:memory:")
    await _apply_schema(conn)
    history = ConversationHistory(conn)
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
async def test_history_append_load_roundtrip_ordering(history_db) -> None:
    _conn, history = history_db
    thread_id = "t1"
    await history.append(thread_id, ChatMessage(role="user", content="U1"))
    await history.append(thread_id, ChatMessage(role="assistant", content="A1"))
    await history.append(thread_id, ChatMessage(role="user", content="U2"))
    loaded = await history.load(thread_id)
    assert [m.content for m in loaded] == ["U1", "A1", "U2"]
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    times = [m.created_at_ms for m in loaded]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_history_load_respects_limit(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-limit"
    for i in range(150):
        await history.append(thread_id, ChatMessage(role="user", content=f"m{i}"))
    loaded = await history.load(thread_id, limit=100)
    assert len(loaded) == 100
    assert loaded[0].content == "m50"
    assert loaded[-1].content == "m149"


@pytest.mark.asyncio
async def test_history_reset_replaces_all_messages(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-reset"
    for i in range(5):
        await history.append(thread_id, ChatMessage(role="user", content=f"m{i}"))
    await history.reset(
        thread_id,
        [
            ChatMessage(role="user", content="a"),
            ChatMessage(role="assistant", content="b"),
        ],
    )
    loaded = await history.load(thread_id)
    assert len(loaded) == 2
    assert [m.content for m in loaded] == ["a", "b"]


@pytest.mark.asyncio
async def test_history_reset_preserves_order(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-order"
    await history.append(thread_id, ChatMessage(role="user", content="old"))
    rows = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="tool", content="t1", tool_call_id="1", tool_name="x"),
    ]
    await history.reset(thread_id, rows)
    loaded = await history.load(thread_id)
    assert [m.role for m in loaded] == ["user", "assistant", "tool"]
    assert loaded[2].tool_call_id == "1"


@pytest.mark.asyncio
async def test_history_reset_with_empty_clears_thread(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-empty"
    await history.append(thread_id, ChatMessage(role="user", content="x"))
    await history.reset(thread_id, [])
    assert await history.load(thread_id) == []


@pytest.mark.asyncio
async def test_history_clear_removes_thread_only(history_db) -> None:
    _conn, history = history_db
    await history.append("t1", ChatMessage(role="user", content="a"))
    await history.append("t2", ChatMessage(role="user", content="b"))
    await history.clear("t1")
    assert await history.load("t1") == []
    t2 = await history.load("t2")
    assert len(t2) == 1
    assert t2[0].content == "b"


@pytest.mark.asyncio
async def test_history_tool_name_roundtrip(history_db) -> None:
    _conn, history = history_db
    thread_id = "t-tool"
    await history.append(thread_id, ChatMessage(role="user", content="hi"))
    await history.append(
        thread_id,
        ChatMessage(
            role="tool",
            content='{"ok":true}',
            tool_call_id="call-1",
            tool_name="my_fn",
        ),
    )
    loaded = await history.load(thread_id)
    assert loaded[1].tool_call_id == "call-1"
    assert loaded[1].tool_name == "my_fn"


@pytest.mark.asyncio
async def test_history_invalid_role_raises(history_db) -> None:
    _conn, history = history_db
    with pytest.raises(ValueError):
        await history.append("t1", ChatMessage(role="system", content="x"))  # type: ignore[arg-type]


def test_sqlite_persistence_source_has_no_google_cloud() -> None:
    root = _repo_root()
    paths = [
        root / "src/monkeybot/core/db.py",
        root / "src/monkeybot/core/history.py",
        root / "src/monkeybot/core/durable_runs.py",
        root / "src/monkeybot/core/usage.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "google.cloud" not in text
        assert "firestore" not in text


def test_importing_db_does_not_import_google_cloud() -> None:
    """Load ``db.py`` in isolation so package ``__init__`` side effects are avoided."""
    root = _repo_root()
    db_path = root / "src/monkeybot/core/db.py"
    before = {k for k in sys.modules if k.startswith("google.cloud")}
    module_name = "_monkeybot_sqlite_db_standalone_test"
    spec = importlib.util.spec_from_file_location(module_name, db_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    after = {k for k in sys.modules if k.startswith("google.cloud")}
    assert after == before
