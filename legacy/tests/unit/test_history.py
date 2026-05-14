from __future__ import annotations

import pytest

try:
    from monkeybot.core.provider import Message
except ImportError:
    from dataclasses import dataclass

    @dataclass
    class Message:  # type: ignore[no-redef]
        role: str
        content: str
        tool_call_id: str | None = None
        tool_name: str | None = None


from monkeybot.core.history import ConversationHistory


@pytest.fixture
async def history(tmp_path):
    h = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await h.init()
    return h


async def test_save_and_load(history: ConversationHistory) -> None:
    await history.save("s1", "user", "Hello")
    await history.save("s1", "assistant", "Hi there")
    msgs = await history.load("s1")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


async def test_load_empty_session(history: ConversationHistory) -> None:
    assert await history.load("nonexistent") == []


async def test_order_is_ascending(history: ConversationHistory) -> None:
    await history.save("s1", "user", "first")
    await history.save("s1", "assistant", "second")
    msgs = await history.load("s1")
    assert msgs[0].content == "first"
    assert msgs[1].content == "second"


async def test_init_idempotent(tmp_path) -> None:
    """Calling init() twice on the same DB does not raise and preserves data."""
    h = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await h.init()
    await h.save("s1", "user", "msg1")
    await h.init()  # second call — must not wipe data
    msgs = await h.load("s1")
    assert len(msgs) == 1
    assert msgs[0].content == "msg1"


async def test_process_restart_persistence(tmp_path) -> None:
    """Data survives creating a new ConversationHistory instance on the same path."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    h1 = ConversationHistory(db_url=db_url)
    await h1.init()
    await h1.save("s1", "user", "persisted")

    h2 = ConversationHistory(db_url=db_url)
    await h2.init()
    msgs = await h2.load("s1")
    assert len(msgs) == 1
    assert msgs[0].content == "persisted"


async def test_tool_call_roundtrip(history: ConversationHistory) -> None:
    """tool_call_id and tool_name are stored and retrieved correctly."""
    await history.save("s1", "tool", "result", tool_call_id="tc-123", tool_name="run_command")
    msgs = await history.load("s1")
    assert len(msgs) == 1
    assert msgs[0].tool_call_id == "tc-123"
    assert msgs[0].tool_name == "run_command"


async def test_null_tool_fields(history: ConversationHistory) -> None:
    """tool_call_id and tool_name default to None when not provided."""
    await history.save("s1", "user", "hello")
    msgs = await history.load("s1")
    assert msgs[0].tool_call_id is None
    assert msgs[0].tool_name is None


async def test_wal_mode_enabled(tmp_path) -> None:
    """After init(), the DB journal_mode is 'wal'."""
    import aiosqlite

    h = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await h.init()
    async with aiosqlite.connect(f"{tmp_path}/test.db") as db:
        async with db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "wal"


async def test_clear_removes_session_messages(history: ConversationHistory) -> None:
    """clear() deletes all messages for a session."""
    await history.save("s1", "user", "msg1")
    await history.save("s1", "assistant", "msg2")
    await history.save("s2", "user", "other")
    await history.clear("s1")
    assert await history.load("s1") == []
    # s2 messages remain untouched
    assert len(await history.load("s2")) == 1


async def test_save_three_messages_load_all(history: ConversationHistory) -> None:
    """Save 3 messages → load returns all 3 in ascending order."""
    await history.save("s1", "user", "a")
    await history.save("s1", "assistant", "b")
    await history.save("s1", "user", "c")
    msgs = await history.load("s1")
    assert len(msgs) == 3
    assert [m.content for m in msgs] == ["a", "b", "c"]


async def test_sessions_are_isolated(history: ConversationHistory) -> None:
    """Messages from different sessions do not bleed into each other."""
    await history.save("s1", "user", "session-one")
    await history.save("s2", "user", "session-two")
    s1_msgs = await history.load("s1")
    s2_msgs = await history.load("s2")
    assert len(s1_msgs) == 1
    assert s1_msgs[0].content == "session-one"
    assert len(s2_msgs) == 1
    assert s2_msgs[0].content == "session-two"
