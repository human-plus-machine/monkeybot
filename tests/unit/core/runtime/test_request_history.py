"""Request-scoped history: one full load per request, checked by a probe."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection
from dataclasses import replace
from typing import Any

import pytest

from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.row_ids import new_row_id
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.persistence.thread_summary import CONTEXT_SUMMARY_PREFIX
from monkeybot.core.runtime.background_summary import reset_pending_summaries
from monkeybot.core.runtime.events import ContextSummarized, Error
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.request_history import RequestHistory
from monkeybot.core.types.content_blocks import ContentBlock, ToolRequest
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.provider import FakeProvider

_THREAD = "t"


def _sig(messages: list[Message]) -> list[tuple[str, str | None, list[dict[str, object]]]]:
    return [
        (message.role, message.row_id, [block.to_dict() for block in message.content])
        for message in messages
    ]


def _payload(message: Message) -> str:
    return json.dumps(
        [block.to_dict() for block in message.content],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _stored(message: Message) -> Message:
    raw = json.loads(_payload(message))
    return Message(
        role=message.role,
        content=[ContentBlock.from_dict(block) for block in raw],
        row_id=message.row_id or new_row_id(),
    )


class MemoryStore:
    """In-memory store with the same insert shape as SQLite, including ``last_row``."""

    def __init__(self) -> None:
        self.rows: dict[str, list[Message]] = {}
        self.ids: dict[str, list[str | None]] = {}
        self.outbox: list[dict[str, Any]] = []
        self.loads = 0
        self.fail: BaseException | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    def _thread(self, thread_id: str) -> tuple[list[Message], list[str | None]]:
        return self.rows.setdefault(thread_id, []), self.ids.setdefault(thread_id, [])

    async def _gate(self) -> None:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()

    def _put(self, thread_id: str, message: Message, message_id: str | None) -> None:
        rows, ids = self._thread(thread_id)
        if message_id is not None and message_id in ids:
            return
        rows.append(_stored(message))
        ids.append(message_id)

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        self.loads += 1
        rows, _ids = self._thread(thread_id)
        chosen = rows if limit is None else (rows[-limit:] if limit > 0 else [])
        return [replace(row, content=list(row.content)) for row in chosen]

    async def last_row(self, thread_id: str) -> tuple[int, str] | None:
        rows, _ids = self._thread(thread_id)
        if not rows:
            return None
        return len(rows), _payload(rows[-1])

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del turn_id
        await self._gate()
        if self.fail is not None:
            raise self.fail
        self._put(thread_id, message, message_id)

    async def append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        del turn_id
        await self._gate()
        if self.fail is not None:
            raise self.fail
        self._put(thread_id, message, message_id)
        self.outbox.append(outbox)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        if self.fail is not None:
            raise self.fail
        self.rows[thread_id] = []
        self.ids[thread_id] = []
        for message in messages:
            self._put(thread_id, message, None)

    async def delete_rows(self, thread_id: str, row_ids: Collection[str]) -> int:
        rows, ids = self._thread(thread_id)
        wanted = set(row_ids)
        kept = [(row, mid) for row, mid in zip(rows, ids, strict=True) if row.row_id not in wanted]
        deleted = len(rows) - len(kept)
        self.rows[thread_id] = [row for row, _mid in kept]
        self.ids[thread_id] = [mid for _row, mid in kept]
        return deleted

    async def list_threads(self, limit: int = 50) -> list[object]:
        del limit
        return []


class BareStore:
    """No ``last_row``. The wrapper must not cache this store."""

    def __init__(self) -> None:
        self.rows: list[Message] = []
        self.loads = 0

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        del thread_id
        self.loads += 1
        if limit is None:
            return list(self.rows)
        return list(self.rows[-limit:]) if limit > 0 else []

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del thread_id, turn_id, message_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.rows = list(messages)


def _tool_row() -> Message:
    return Message(
        role="assistant",
        content=[ToolRequest(id="c1", name="run_command", args={"n": 1})],
    )


@pytest.mark.asyncio
async def test_snapshot_matches_store_after_append_outbox_and_reset() -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.append(_THREAD, Message.text("user", "one"))
    await wrapped.append_with_outbox(
        _THREAD,
        _tool_row(),
        turn_id="turn-1",
        message_id="m-tool",
        outbox={"message_id": "m-tool"},
    )
    assert store.outbox == [{"message_id": "m-tool"}]

    first = await wrapped.load(_THREAD)
    assert _sig(first) == _sig(store.rows[_THREAD])
    assert all(message.row_id for message in first)
    assert store.loads == 1

    second = await wrapped.load(_THREAD)
    assert _sig(second) == _sig(first)
    assert store.loads == 1
    second[0].content.clear()
    kept = await wrapped.load(_THREAD)
    assert [block.text for message in kept for block in message.content if hasattr(block, "text")]
    assert store.loads == 1

    await wrapped.reset(
        _THREAD,
        [Message.text("user", "kept"), Message.text("assistant", "tail")],
    )
    assert store.loads == 1
    reset = await wrapped.load(_THREAD)
    assert _sig(reset) == _sig(store.rows[_THREAD])
    assert [block.text for message in reset for block in message.content] == ["kept", "tail"]
    assert store.loads == 1


@pytest.mark.asyncio
async def test_duplicate_message_id_reloads_instead_of_serving_a_phantom_row() -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.load(_THREAD)
    await wrapped.append(_THREAD, Message.text("user", "one"), message_id="same")
    await wrapped.append(_THREAD, Message.text("user", "two"), message_id="same")

    loaded = await wrapped.load(_THREAD)
    assert _sig(loaded) == _sig(store.rows[_THREAD])
    assert [block.text for message in loaded for block in message.content] == ["one"]
    assert store.loads == 2


@pytest.mark.asyncio
async def test_outside_append_is_reloaded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.load(_THREAD)
    await store.append(_THREAD, Message.text("user", "from-voice"))

    with caplog.at_level("WARNING"):
        loaded = await wrapped.load(_THREAD)

    assert [block.text for message in loaded for block in message.content] == ["from-voice"]
    assert store.loads == 2
    assert "request history snapshot stale" in caplog.text


@pytest.mark.asyncio
async def test_failed_write_drops_the_snapshot() -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.append(_THREAD, Message.text("user", "kept"))
    await wrapped.load(_THREAD)
    assert store.loads == 1
    store.fail = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await wrapped.append(_THREAD, Message.text("user", "lost"))

    store.fail = None
    loaded = await wrapped.load(_THREAD)
    assert [block.text for message in loaded for block in message.content] == ["kept"]
    assert store.loads == 2


@pytest.mark.asyncio
async def test_cancelled_write_drops_the_snapshot() -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.load(_THREAD)
    store.entered = asyncio.Event()
    store.release = asyncio.Event()
    task = asyncio.create_task(wrapped.append(_THREAD, Message.text("user", "cut")))
    await store.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    store.entered = None
    store.release = None
    await wrapped.load(_THREAD)
    assert store.loads == 2
    assert store.rows.get(_THREAD, []) == []


@pytest.mark.asyncio
async def test_concurrent_append_and_load_do_not_duplicate() -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.load(_THREAD)
    store.entered = asyncio.Event()
    store.release = asyncio.Event()
    appender = asyncio.create_task(wrapped.append(_THREAD, Message.text("user", "one")))
    await store.entered.wait()
    loader = asyncio.create_task(wrapped.load(_THREAD))
    await asyncio.sleep(0)
    store.release.set()
    await appender
    loaded = await loader

    assert [block.text for message in loaded for block in message.content] == ["one"]
    assert len(store.rows[_THREAD]) == 1
    assert store.loads == 1


@pytest.mark.asyncio
async def test_store_without_last_row_is_passed_through() -> None:
    store = BareStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    message = Message.text("user", "hi")
    await wrapped.append(_THREAD, message)

    assert store.rows[0] is message
    assert message.row_id is None
    assert not hasattr(wrapped, "append_with_outbox")
    assert await wrapped.load(_THREAD) == [message]
    assert await wrapped.load(_THREAD) == [message]
    assert store.loads == 2


@pytest.mark.asyncio
async def test_limited_and_other_thread_loads_do_not_use_the_snapshot() -> None:
    store = MemoryStore()
    wrapped = RequestHistory.wrap(store, thread_id=_THREAD)
    await wrapped.append(_THREAD, Message.text("user", "here"))
    await wrapped.load(_THREAD)
    assert store.loads == 1

    limited = await wrapped.load(_THREAD, limit=1)
    assert [block.text for message in limited for block in message.content] == ["here"]
    await wrapped.load("other")
    await wrapped.append("other", Message.text("user", "elsewhere"))
    await wrapped.load(_THREAD)

    assert store.loads == 3
    assert [block.text for message in store.rows["other"] for block in message.content] == [
        "elsewhere"
    ]


class CountingHistory:
    def __init__(self, inner: SQLiteHistoryStore) -> None:
        self.inner = inner
        self.loads = 0
        self.probes = 0

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        self.loads += 1
        return await self.inner.load(thread_id, limit)

    async def last_row(self, thread_id: str) -> tuple[int, str] | None:
        self.probes += 1
        return await self.inner.last_row(thread_id)

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        await self.inner.append(thread_id, message, turn_id=turn_id, message_id=message_id)

    async def append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        await self.inner.append_with_outbox(
            thread_id,
            message,
            turn_id=turn_id,
            message_id=message_id,
            outbox=outbox,
        )

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        await self.inner.reset(thread_id, messages)

    async def delete_rows(self, thread_id: str, row_ids: Collection[str]) -> int:
        return await self.inner.delete_rows(thread_id, row_ids)

    async def list_threads(self, limit: int = 50) -> list[object]:
        return await self.inner.list_threads(limit)


class NoProbe:
    """Hides ``last_row`` so the same SQLite store takes the pass-through path."""

    def __init__(self, inner: SQLiteHistoryStore) -> None:
        self.inner = inner

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        return await self.inner.load(thread_id, limit)

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        await self.inner.append(thread_id, message, turn_id=turn_id, message_id=message_id)

    async def append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        await self.inner.append_with_outbox(
            thread_id,
            message,
            turn_id=turn_id,
            message_id=message_id,
            outbox=outbox,
        )

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        await self.inner.reset(thread_id, messages)

    async def delete_rows(self, thread_id: str, row_ids: Collection[str]) -> int:
        return await self.inner.delete_rows(thread_id, row_ids)

    async def list_threads(self, limit: int = 50) -> list[object]:
        return await self.inner.list_threads(limit)


def _tool_script() -> list[list[object]]:
    turns: list[list[object]] = []
    for index in range(3):
        turns.append(
            [
                ToolCall(call_id=f"c{index}", name="run_command", args={"command": str(index)}),
                Done(),
            ]
        )
    turns.append([TextDelta(text="done"), Done()])
    return turns


def _wire(provider: FakeProvider) -> list[list[tuple[str, list[dict[str, object]]]]]:
    return [
        [(message.role, [block.to_dict() for block in message.content]) for message in call]
        for call in provider.stream_messages
    ]


async def _sqlite() -> tuple[SQLiteHistoryStore, Any]:
    conn = await open_connection("sqlite:///:memory:")
    await apply_schema(conn)
    return SQLiteHistoryStore(conn), conn


async def _seed(store: SQLiteHistoryStore, count: int) -> None:
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        await store.append("t1", Message.text(role, f"row {index}"))


async def _drain(history: object, provider: FakeProvider, *, window: int = 200_000) -> list[object]:
    return [
        event
        async for event in run(
            "hello",
            _ctx(context_window_tokens=window),
            provider=provider,
            history=history,  # type: ignore[arg-type]
            inspectors=[],
            tool_executor=RecordingExecutor(),
            max_turns=6,
        )
    ]


class BandProvider(FakeProvider):
    """Reports a prompt in the 70–85% band. Summary calls carry no tools."""

    def __init__(self) -> None:
        super().__init__(
            [
                [ToolCall(call_id="c1", name="run_command", args={"command": "true"}), Done()],
                [TextDelta(text="final"), Done()],
            ]
        )

    async def count_input_tokens(
        self,
        messages: list[Message],
        tools: list[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> int:
        del messages, tools, model, thinking_budget, vertex_google_search
        return 750

    async def stream(
        self,
        messages: list[Message],
        tools: list[object],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ):
        if not tools:
            yield TextDelta(text="summary of the middle")
            yield Done()
            return
        async for event in super().stream(
            messages,
            tools,  # type: ignore[arg-type]
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        ):
            yield event


@pytest.mark.asyncio
async def test_three_tool_turns_load_history_once() -> None:
    store, conn = await _sqlite()
    try:
        await _seed(store, 6)
        counted = CountingHistory(store)
        provider = FakeProvider(_tool_script())
        events = await _drain(counted, provider)
        assert not any(isinstance(event, Error) for event in events)
        assert provider.stream_calls == 4
        assert counted.loads == 1
        assert counted.probes >= 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_band_turn_with_compaction_loads_history_once() -> None:
    store, conn = await _sqlite()
    try:
        await _seed(store, 8)
        counted = CountingHistory(store)
        provider = BandProvider()
        events = await _drain(counted, provider, window=1000)
        summarized = [event for event in events if isinstance(event, ContextSummarized)]
        assert summarized and any(event.turns_summarized > 0 for event in summarized)
        assert not any(isinstance(event, Error) for event in events)
        assert counted.loads == 1
        stored = await store.load("t1")
        assert any(CONTEXT_SUMMARY_PREFIX in block.text for row in stored for block in row.content)
    finally:
        await reset_pending_summaries()
        await conn.close()


@pytest.mark.asyncio
async def test_provider_requests_match_with_and_without_the_snapshot() -> None:
    on_store, on_conn = await _sqlite()
    off_store, off_conn = await _sqlite()
    try:
        await _seed(on_store, 4)
        await _seed(off_store, 4)
        on_provider = FakeProvider(_tool_script())
        off_provider = FakeProvider(_tool_script())
        on_events = await _drain(CountingHistory(on_store), on_provider)
        off_events = await _drain(NoProbe(off_store), off_provider)
        assert not any(isinstance(event, Error) for event in (*on_events, *off_events))
        assert _wire(on_provider) == _wire(off_provider)
    finally:
        await on_conn.close()
        await off_conn.close()
