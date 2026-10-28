"""FakeHistory covers the HistoryStore methods the loop tests call."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message
from testkit.history import FakeHistory


async def test_load_returns_preload_and_honors_limit() -> None:
    history = FakeHistory(
        [
            Message.text("user", "a"),
            Message.text("user", "b"),
            Message.text("user", "c"),
        ]
    )
    assert [message.content[0].text for message in await history.load("t")] == ["a", "b", "c"]
    assert [message.content[0].text for message in await history.load("t", limit=1)] == ["c"]
    assert await history.load("t", limit=0) == []


async def test_append_records_the_message() -> None:
    history = FakeHistory()
    message = Message.text("user", "hi")
    await history.append("thread", message, turn_id="turn", message_id="m1")
    assert history.rows == [message]
    assert await history.load("other-thread") == [message]


async def test_reset_replaces_rows_and_records_the_call() -> None:
    history = FakeHistory([Message.text("user", "old")])
    fresh = [Message.text("assistant", "new")]
    await history.reset("t1", fresh)
    assert history.reset_calls == [("t1", fresh)]
    assert await history.load("t1") == fresh
