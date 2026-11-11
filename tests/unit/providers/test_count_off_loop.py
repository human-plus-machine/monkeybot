"""The tiktoken count must not stall the event loop."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.providers._openai_compat import count_input_tokens_tiktoken


class _SlowEncoding:
    """Stand-in whose encode sleeps, and records which thread ran it."""

    def __init__(self, seen: list[tuple[str, int]]) -> None:
        self._seen = seen

    def encode(self, text: str) -> list[int]:
        del text
        self._seen.append(("encode", threading.get_ident()))
        time.sleep(0.5)
        return [1]


async def test_token_count_does_not_stall_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 0.5 s encode must leave the loop free to tick every 10 ms, off the loop thread."""
    seen: list[tuple[str, int]] = []
    loop_thread = threading.get_ident()

    def lookup(model: str) -> _SlowEncoding:
        del model
        seen.append(("lookup", threading.get_ident()))
        return _SlowEncoding(seen)

    monkeypatch.setattr("monkeybot.providers._openai_compat.tiktoken_encoding_for_model", lookup)

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
    await count_input_tokens_tiktoken(
        [Message.text("user", "count these tokens")],
        [],
        model="llama-off-loop",
        provider="ollama",
        max_request_bytes=8 * 1024 * 1024,
    )
    stop.set()
    await ticker

    assert gaps
    assert max(gaps) < 0.1
    # The role string and the message text are encoded separately. Both, and
    # the encoding lookup, have to be off the loop thread.
    assert seen[0][0] == "lookup"
    assert {name for name, _thread in seen} == {"lookup", "encode"}
    assert all(thread != loop_thread for _name, thread in seen)
