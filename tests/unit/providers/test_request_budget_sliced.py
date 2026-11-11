"""The yielding size pass matches the sync one, and does not hold the loop."""

from __future__ import annotations

import asyncio
import time

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import File, Image, Text, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers import request_budget
from monkeybot.providers.request_budget import (
    _request_bytes,
    request_bytes_sliced,
    trim_message_media_for_byte_budget,
)


def _messages() -> list[Message]:
    return [
        Message(role="user", content=[Text(text="plain")]),
        Message(
            role="user",
            content=[Image(mime_type="image/png", data="A" * 100, metadata={"path": "a.png"})],
        ),
        Message(
            role="user",
            content=[File(mime_type="application/pdf", data="B" * 100)],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="load_file",
                    result=[Text(text="loaded"), Image(mime_type="image/png", data="C" * 40)],
                )
            ],
        ),
    ]


def _tools() -> list[ToolDef]:
    return [ToolDef("load_file", "Load a file", {"type": "object"})]


async def test_sliced_size_matches_the_sync_count() -> None:
    messages = _messages()
    tools = _tools()
    for count_file_data in (True, False):
        sliced = await request_bytes_sliced(messages, tools, count_file_data=count_file_data)
        assert sliced == _request_bytes(messages, tools, count_file_data=count_file_data)


async def test_known_bytes_trims_the_same_list() -> None:
    messages = _messages()
    tools = _tools()
    size = await request_bytes_sliced(messages, tools)
    with_size = trim_message_media_for_byte_budget(
        messages,
        tools,
        max_bytes=4_000,
        provider="gemini",
        raise_if_oversized=False,
        known_bytes=size,
    )
    measured = trim_message_media_for_byte_budget(
        messages,
        tools,
        max_bytes=4_000,
        provider="gemini",
        raise_if_oversized=False,
    )
    assert with_size == measured


async def test_sliced_size_does_not_stall_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight slow messages yield between them, so no gap reaches their sum."""
    real = request_budget._message_bytes

    def slow(message: Message, *, count_file_data: bool) -> int:
        time.sleep(0.05)
        return real(message, count_file_data=count_file_data)

    monkeypatch.setattr(request_budget, "_message_bytes", slow)
    messages = [Message(role="user", content=[Text(text=f"row {index}")]) for index in range(8)]

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
    await request_bytes_sliced(messages, [])
    stop.set()
    await ticker

    assert gaps
    assert max(gaps) < 0.1
