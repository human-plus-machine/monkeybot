"""Decoding a history load hands the event loop back between rows."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.types.content_blocks import ContentBlock, Text


async def test_sqlite_load_does_not_stall_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow decode of each row yields, and the loaded rows are unchanged."""
    conn = await open_connection("sqlite:///:memory:")
    await apply_schema(conn)
    store = SQLiteHistoryStore(conn)
    try:
        for index in range(8):
            await store.append("thread", Message(role="user", content=[Text(text=f"row {index}")]))

        real = ContentBlock.from_dict

        def slow(payload: dict[str, Any]) -> ContentBlock:
            time.sleep(0.05)
            return real(payload)

        monkeypatch.setattr("monkeybot.core.persistence.history.ContentBlock.from_dict", slow)

        gaps: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        loaded = await store.load("thread")
        stop.set()
        await ticker
    finally:
        await conn.close()

    assert [block.text for message in loaded for block in message.content] == [
        f"row {index}" for index in range(8)
    ]
    assert gaps
    assert max(gaps) < 0.1
