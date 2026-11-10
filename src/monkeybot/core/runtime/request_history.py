"""Request-scoped snapshot of one thread's history.

A turn reloads the whole thread several times. This wrapper keeps the rows from
the first full load and serves later loads from that snapshot after a ``last_row``
probe. The probe is there because a voice call can append to the same thread
without the turn lock. A store with no ``last_row`` is passed through unchanged,
so the snapshot is never served unverified.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import replace
from typing import Any, cast

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import ArchivedRow, HistoryStore
from monkeybot.core.persistence.row_ids import new_row_id
from monkeybot.core.persistence.thread_summary import ChatThreadSummary
from monkeybot.core.types.content_blocks import ContentBlock

logger = logging.getLogger(__name__)


def _normalize(message: Message) -> Message:
    """The row a store would hand back after writing ``message``.

    The stored ``row_id`` is assigned on insert, and blocks come back through
    ``from_dict``. Doing both before the write makes the snapshot equal a fresh
    load, including the id.
    """
    blocks = [ContentBlock.from_dict(block.to_dict()) for block in message.content]
    return Message(role=message.role, content=blocks, row_id=message.row_id or new_row_id())


def _copy_rows(rows: list[Message]) -> list[Message]:
    """A list the caller can mutate without changing the snapshot."""
    return [replace(row, content=list(row.content)) for row in rows]


def _same_newest(message: Message, stored: str) -> bool:
    """True when ``stored`` is the JSON of ``message`` content.

    Parsed rather than compared as text: a backend may re-serialise the JSON
    and still return the same row.
    """
    try:
        parsed = json.loads(stored)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return bool(parsed == [block.to_dict() for block in message.content])


class RequestHistory:
    """``HistoryStore`` for one thread for the duration of one request.

    Writes go to the inner store and then to the snapshot. A write that raises
    or is cancelled drops the snapshot, and the next load reads the store.
    """

    def __init__(self, inner: HistoryStore, thread_id: str) -> None:
        self._inner = inner
        self._thread_id = thread_id
        self._rows: list[Message] | None = None
        self._lock = asyncio.Lock()
        # A snapshot served without a probe can miss a voice-call append and
        # then be written back by compaction, deleting that append.
        self._can_probe = callable(getattr(inner, "last_row", None))
        if callable(getattr(inner, "append_with_outbox", None)):
            self.append_with_outbox = self._append_with_outbox

    @classmethod
    def wrap(cls, inner: HistoryStore, *, thread_id: str) -> RequestHistory:
        return cls(inner, thread_id)

    def _tracks(self, thread_id: str) -> bool:
        return self._can_probe and thread_id == self._thread_id

    def _drop(self) -> None:
        self._rows = None

    async def _is_current(self) -> bool:
        assert self._rows is not None
        probed = await self._inner.last_row(self._thread_id)
        rows = self._rows
        if probed is None:
            current = not rows
            store_rows = 0
        else:
            store_rows, content = probed
            current = store_rows == len(rows) and bool(rows) and _same_newest(rows[-1], content)
        if not current:
            logger.warning(
                "request history snapshot stale %s",
                kv(
                    thread_id=self._thread_id,
                    snapshot_rows=len(rows),
                    store_rows=store_rows,
                ),
            )
        return current

    async def _fill(self) -> list[Message]:
        rows = await self._inner.load(self._thread_id)
        self._rows = rows
        return _copy_rows(rows)

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        async with self._lock:
            if not self._tracks(thread_id) or limit is not None:
                return await self._inner.load(thread_id, limit)
            if self._rows is None:
                return await self._fill()
            if await self._is_current():
                return _copy_rows(self._rows)
            return await self._fill()

    async def _commit(
        self,
        thread_id: str,
        write: Callable[[], Awaitable[None]],
        remember: Callable[[], None] | None,
    ) -> None:
        try:
            await write()
        except BaseException:
            if thread_id == self._thread_id:
                self._drop()
            raise
        if remember is not None:
            remember()

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        async with self._lock:
            if not self._tracks(thread_id):
                await self._commit(
                    thread_id,
                    lambda: self._inner.append(
                        thread_id, message, turn_id=turn_id, message_id=message_id
                    ),
                    None,
                )
                return
            normalized = _normalize(message)

            def remember() -> None:
                # No snapshot yet: the next load reads the store, which already
                # includes this row. Extending here would keep a partial thread.
                if self._rows is not None:
                    self._rows.append(normalized)

            await self._commit(
                thread_id,
                lambda: self._inner.append(
                    thread_id, normalized, turn_id=turn_id, message_id=message_id
                ),
                remember,
            )

    async def _append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        async with self._lock:
            store = cast(Any, self._inner)
            if not self._tracks(thread_id):
                await self._commit(
                    thread_id,
                    lambda: store.append_with_outbox(
                        thread_id,
                        message,
                        turn_id=turn_id,
                        message_id=message_id,
                        outbox=outbox,
                    ),
                    None,
                )
                return
            normalized = _normalize(message)

            def remember() -> None:
                if self._rows is not None:
                    self._rows.append(normalized)

            await self._commit(
                thread_id,
                lambda: store.append_with_outbox(
                    thread_id,
                    normalized,
                    turn_id=turn_id,
                    message_id=message_id,
                    outbox=outbox,
                ),
                remember,
            )

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        async with self._lock:
            if not self._tracks(thread_id):
                await self._commit(
                    thread_id,
                    lambda: self._inner.reset(thread_id, messages),
                    None,
                )
                return
            normalized = [_normalize(message) for message in messages]

            def remember() -> None:
                self._rows = normalized

            await self._commit(
                thread_id,
                lambda: self._inner.reset(thread_id, normalized),
                remember,
            )

    async def reset_archiving(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        reason: str,
        summary_row_id: str | None,
    ) -> int:
        """Pass an archiving rewrite through, and keep the snapshot in step.

        A store without the method is rewritten with ``reset``. The dropped
        rows are not kept, and the warning says so.
        """
        archive = getattr(self._inner, "reset_archiving", None)
        async with self._lock:
            if not self._tracks(thread_id):
                if not callable(archive):
                    logger.warning(
                        "history store has no archive; dropped rows will not be recoverable %s",
                        kv(thread_id=thread_id, reason=reason),
                    )
                    await self._inner.reset(thread_id, messages)
                    return 0
                archived: int = await archive(
                    thread_id,
                    messages,
                    reason=reason,
                    summary_row_id=summary_row_id,
                )
                return archived
            normalized = [_normalize(message) for message in messages]
            try:
                if not callable(archive):
                    logger.warning(
                        "history store has no archive; dropped rows will not be recoverable %s",
                        kv(thread_id=thread_id, reason=reason),
                    )
                    await self._inner.reset(thread_id, normalized)
                    archived = 0
                else:
                    archived = await archive(
                        thread_id,
                        normalized,
                        reason=reason,
                        summary_row_id=summary_row_id,
                    )
            except BaseException:
                self._drop()
                raise
            self._rows = normalized
            return archived

    async def load_archive(self, thread_id: str) -> list[ArchivedRow]:
        return await self._inner.load_archive(thread_id)

    async def delete_archive(self, thread_id: str) -> int:
        return await self._inner.delete_archive(thread_id)

    async def delete_rows(self, thread_id: str, row_ids: Collection[str]) -> int:
        async with self._lock:
            # Drop before the delete. A partial delete must not leave the old rows.
            if thread_id == self._thread_id:
                self._drop()
            return await self._inner.delete_rows(thread_id, row_ids)

    async def last_row(self, thread_id: str) -> tuple[int, str] | None:
        return await self._inner.last_row(thread_id)

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        return await self._inner.list_threads(limit)
