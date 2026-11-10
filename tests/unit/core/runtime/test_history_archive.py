"""Compaction and load-max truncation keep the rows they drop."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.persistence.archive import unmatched_indexes
from monkeybot.core.persistence.branches import BranchRecord
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.runtime import background_summary as bg
from monkeybot.core.runtime import turn_loop
from monkeybot.core.runtime.background_summary import (
    apply_pending_summary,
    maybe_start,
    reset_pending_summaries,
    wait_idle,
)
from monkeybot.core.runtime.context_budget import PRESSURE_MODERATE_RATIO, SUMMARY_TRIGGER_RATIO
from monkeybot.core.runtime.doom_loop import _DoomLoopTracker
from monkeybot.core.runtime.history_compaction import _summarize_history
from monkeybot.core.runtime.history_rewrite import purge_session_branches
from monkeybot.core.runtime.request_history import RequestHistory
from monkeybot.core.runtime.turn_loop import _apply_history_load_max_safety, _TurnState
from monkeybot.core.types.content_blocks import SystemNotification, Text


def _ctx(thread_id: str, window: int = 100) -> TurnContext:
    return TurnContext(
        thread_id=thread_id,
        request_id="r1",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        context_window_tokens=window,
    )


class _SummaryProvider:
    def __init__(self, text: str = "summary") -> None:
        self.text = text
        self.calls = 0

    async def stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        self.calls += 1
        yield TextDelta(text=self.text)
        yield Done()


class _BareHistory:
    """A store that can be rewritten but has no archive."""

    def __init__(self) -> None:
        self.rows: list[Message] = []

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        del thread_id
        if limit is None:
            return list(self.rows)
        return list(self.rows[-limit:]) if limit > 0 else []

    async def append(self, thread_id: str, message: Message, **_kwargs: Any) -> None:
        del thread_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.rows = list(messages)


class _Branches:
    def __init__(self, record: BranchRecord) -> None:
        self._record = record

    async def list(self, session_id: str) -> list[BranchRecord]:
        assert session_id == self._record.session_id
        return [self._record]

    async def delete_session(self, session_id: str) -> list[BranchRecord]:
        del session_id
        return []


class _CountLoads:
    def __init__(self, inner: SQLiteHistoryStore) -> None:
        self._inner = inner
        self.loads = 0

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        self.loads += 1
        return await self._inner.load(thread_id, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _row_sig(messages: list[Message]) -> list[tuple[str, str | None, list[dict[str, object]]]]:
    return [
        (message.role, message.row_id, [block.to_dict() for block in message.content])
        for message in messages
    ]


@pytest_asyncio.fixture
async def store():
    conn = await open_connection("sqlite:///:memory:")
    await apply_schema(conn)
    history = SQLiteHistoryStore(conn)
    yield history
    await conn.close()


@pytest.fixture(autouse=True)
async def _clear_pending() -> AsyncIterator[None]:
    await reset_pending_summaries()
    yield
    await reset_pending_summaries()


def test_unmatched_indexes_matches_by_count() -> None:
    assert unmatched_indexes(["a", "b", "a", "c"], ["a", "c"]) == [1, 2]
    assert unmatched_indexes(["a", "b"], []) == [0, 1]
    assert unmatched_indexes(["a"], ["a", "missing"]) == []


async def _seed_compactable(store: SQLiteHistoryStore, thread_id: str) -> list[Message]:
    for index in range(4):
        await store.append(
            thread_id,
            Message.text("user" if index % 2 == 0 else "assistant", "x" * 400 + str(index)),
        )
    await store.append(
        thread_id,
        Message(
            role="user",
            content=[SystemNotification(notification_type="verifierVerdict", msg="ok")],
        ),
    )
    for index in range(4, 20):
        await store.append(
            thread_id,
            Message.text("user" if index % 2 == 0 else "assistant", "x" * 400 + str(index)),
        )
    return await store.load(thread_id)


def _summary_row(before: list[Message], after: list[Message]) -> Message:
    known = {message.row_id for message in before}
    new = [message for message in after if message.row_id not in known]
    assert len(new) == 1
    return new[0]


async def test_inline_compaction_archives_the_middle(store: SQLiteHistoryStore) -> None:
    before = await _seed_compactable(store, "inline")
    summarized = await _summarize_history(
        "inline",
        store,
        _SummaryProvider(),  # type: ignore[arg-type]
        "m",
        window_tokens=100,
    )
    assert summarized > 0
    after = await store.load("inline")
    archived = await store.load_archive("inline")
    verdict_id = next(
        message.row_id
        for message in before
        if any(isinstance(block, SystemNotification) for block in message.content)
    )
    assert verdict_id in {message.row_id for message in after}
    assert verdict_id not in {row.message.row_id for row in archived}
    dropped = [message for message in before if message.row_id not in {m.row_id for m in after}]
    assert dropped
    assert [row.message.row_id for row in archived] == [message.row_id for message in dropped]
    summary = _summary_row(before, after)
    assert all(row.summary_row_id == summary.row_id for row in archived)
    assert all(row.reason == "compaction" for row in archived)
    assert len({row.archive_id for row in archived}) == 1
    assert any(isinstance(block, Text) and "summary" in block.text for block in summary.content)


async def test_background_summary_archives_the_middle(store: SQLiteHistoryStore) -> None:
    before = await _seed_compactable(store, "background")
    window = 100
    floor = max(1, int(window * PRESSURE_MODERATE_RATIO))
    cap = max(1, int(window * SUMMARY_TRIGGER_RATIO))
    preflight = (floor + cap) // 2
    assert floor <= preflight < cap
    provider = _SummaryProvider("background summary")
    ctx = _ctx("background", window)
    await maybe_start(ctx, store, provider, preflight)  # type: ignore[arg-type]
    await wait_idle("background")
    applied = await apply_pending_summary(
        thread_id="background",
        history=store,
        model="m",
        window_tokens=window,
        workspace_trailer=None,
    )
    assert applied
    assert provider.calls >= 1
    after = await store.load("background")
    archived = await store.load_archive("background")
    dropped = [message for message in before if message.row_id not in {m.row_id for m in after}]
    assert [row.message.row_id for row in archived] == [message.row_id for message in dropped]
    summary = _summary_row(before, after)
    assert all(
        row.summary_row_id == summary.row_id and row.reason == "compaction" for row in archived
    )
    assert bg._pending.get("background") is None


async def test_load_max_archives_the_dropped_prefix(
    store: SQLiteHistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turn_loop, "HISTORY_LOAD_MAX", 2)

    async def _quiet(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        if False:
            yield None

    monkeypatch.setattr(turn_loop, "_refresh_prompt_after_history_change", _quiet)
    thread = "loadmax"
    for index in range(5):
        await store.append(thread, Message.text("user", f"m{index}"))
    raw = await store.load(thread)
    state = _TurnState(
        ctx=_ctx(thread),
        user_text="",
        effective_max=1,
        doom_tracker=_DoomLoopTracker(threshold=3),
        chat_messages=list(raw),
    )
    events = [
        event
        async for event in _apply_history_load_max_safety(
            state,
            history=store,
            attachment_store=None,
            attachment_catalog=None,
        )
    ]
    assert any(getattr(event, "intervention", None) == "compaction_fallback" for event in events)
    live = await store.load(thread)
    archived = await store.load_archive(thread)
    assert [message.row_id for message in live] == [message.row_id for message in raw[-2:]]
    assert [row.message.row_id for row in archived] == [message.row_id for message in raw[:3]]
    assert [row.position for row in archived] == [0, 1, 2]
    assert all(row.reason == "load_max" and row.summary_row_id is None for row in archived)


async def test_store_without_archive_still_compacts(caplog: pytest.LogCaptureFixture) -> None:
    history = _BareHistory()
    for index in range(20):
        history.rows.append(
            Message.text("user" if index % 2 == 0 else "assistant", "x" * 400 + str(index))
        )
    with caplog.at_level(logging.WARNING, logger="monkeybot.core.runtime.loop.history_compaction"):
        summarized = await _summarize_history(
            "bare",
            history,  # type: ignore[arg-type]
            _SummaryProvider(),  # type: ignore[arg-type]
            "m",
            window_tokens=100,
        )
    assert summarized > 0
    assert any("summary" in block.text for message in history.rows for block in message.content)
    assert "dropped rows will not be recoverable" in caplog.text


async def test_request_history_snapshot_matches_after_archive(store: SQLiteHistoryStore) -> None:
    await _seed_compactable(store, "snap")
    counted = _CountLoads(store)
    wrapped = RequestHistory.wrap(counted, thread_id="snap")
    summarized = await _summarize_history(
        "snap",
        wrapped,
        _SummaryProvider(),  # type: ignore[arg-type]
        "m",
        window_tokens=100,
    )
    assert summarized > 0
    assert counted.loads == 1
    snap = await wrapped.load("snap")
    assert counted.loads == 1
    assert _row_sig(snap) == _row_sig(await store.load("snap"))
    archived = await store.load_archive("snap")
    assert archived
    assert all(row.reason == "compaction" for row in archived)


async def test_purge_deletes_the_archive(store: SQLiteHistoryStore) -> None:
    session = "session"
    branch = "branch:session:b1"

    async def _drop_one(thread_id: str) -> None:
        await store.append(thread_id, Message.text("user", "keep"))
        await store.append(thread_id, Message.text("assistant", "drop"))
        loaded = await store.load(thread_id)
        await store.reset_archiving(thread_id, [loaded[0]], reason="load_max", summary_row_id=None)
        assert await store.load_archive(thread_id)

    await _drop_one(session)
    await _drop_one(branch)
    record = BranchRecord(
        branch_id="b1",
        session_id=session,
        thread_id=branch,
        parent_branch_id="root",
        fork_row_id=None,
        op="edit",
        created_at=1,
        last_active_at=1,
        is_active=False,
    )
    await purge_session_branches(store, _Branches(record), session)  # type: ignore[arg-type]
    assert await store.load(session) == []
    assert await store.load(branch) == []
    assert await store.load_archive(session) == []
    assert await store.load_archive(branch) == []


async def test_failed_rewrite_rolls_back_the_archive(store: SQLiteHistoryStore) -> None:
    thread = "rollback"
    await store.append(thread, Message.text("user", "one"))
    await store.append(thread, Message.text("assistant", "two"))
    before = await store.load(thread)

    async def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("forced archive failure")

    store._insert_message = _fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="forced archive failure"):
        await store.reset_archiving(thread, [before[0]], reason="load_max", summary_row_id=None)
    assert [message.row_id for message in await store.load(thread)] == [
        message.row_id for message in before
    ]
    assert await store.load_archive(thread) == []


async def test_duplicate_row_id_archives_only_the_extra(store: SQLiteHistoryStore) -> None:
    thread = "dup"
    shared = "same-row"
    await store.append(thread, Message(role="user", content=[Text(text="kept")], row_id=shared))
    await store.append(thread, Message(role="user", content=[Text(text="extra")], row_id=shared))
    loaded = await store.load(thread)
    assert [message.row_id for message in loaded] == [shared, shared]
    archived_count = await store.reset_archiving(
        thread, [loaded[0]], reason="load_max", summary_row_id=None
    )
    assert archived_count == 1
    archived = await store.load_archive(thread)
    assert len(archived) == 1
    assert archived[0].position == 1
    assert archived[0].message.content[0].text == "extra"  # type: ignore[union-attr]
    live = await store.load(thread)
    assert len(live) == 1
    assert live[0].content[0].text == "kept"  # type: ignore[union-attr]


async def test_plain_reset_does_not_archive(store: SQLiteHistoryStore) -> None:
    thread = "plain"
    await store.append(thread, Message.text("user", "one"))
    await store.append(thread, Message.text("assistant", "two"))
    loaded = await store.load(thread)
    await store.reset(thread, [loaded[0]])
    assert len(await store.load(thread)) == 1
    assert await store.load_archive(thread) == []
