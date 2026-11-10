"""Row metadata carried through ``history.reset``.

``created_at``, ``turn_id``, and ``message_id`` stay on the stored row. A
rewrite matches survivors by ``row_id`` and reuses what it found. A new row,
and a copy onto a thread that has no stored rows, gets a fresh stamp and no
ids. ``message_id`` is never copied: it is unique across threads, and the
insert path treats an existing one as "already written."
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from monkeybot.core.llm.provider import Message


@dataclass(frozen=True)
class RowMeta:
    """Metadata of one stored history row. ``created_at`` is epoch milliseconds."""

    created_at: int | None = None
    turn_id: str | None = None
    message_id: str | None = None


def stored_text(value: object) -> str | None:
    """A stored id column, or None when it is missing or blank."""
    if isinstance(value, str) and value:
        return value
    return None


def match_stored(
    messages: Sequence[Message],
    stored: Sequence[tuple[str, RowMeta]],
) -> list[int | None]:
    """Index into ``stored`` for each message, or None when the message is new.

    ``stored`` is in load order. Duplicate ``row_id``s pair in that order, each
    stored row at most once. A message with no ``row_id`` never matches.
    """
    buckets: dict[str, deque[int]] = {}
    for index, (row_id, _meta) in enumerate(stored):
        if row_id:
            buckets.setdefault(row_id, deque()).append(index)
    matched: list[int | None] = []
    for message in messages:
        queue = buckets.get(message.row_id) if message.row_id else None
        matched.append(queue.popleft() if queue else None)
    return matched


def carry_created_at(
    carried: Sequence[int | None],
    *,
    now_ms: int,
    step: int,
) -> list[int]:
    """Stamps for a rewritten thread, one per row, in row order.

    A number in ``carried`` is the matched row's stamp; None is a new row.
    Order wins: a stamp is never below the previous stamp plus ``step``.
    A matched row keeps its stamp when that still holds. A new row takes the
    previous stamp plus ``step``, and leading new rows count back from the
    first known stamp. With no known stamp, the rows end at ``now_ms``.

    ``step`` is 0 where the storage key breaks ties (SQLite, Postgres) and 1
    where ``created_at`` is the only order (Firestore).
    """
    count = len(carried)
    if count == 0:
        return []
    known = [index for index, stamp in enumerate(carried) if stamp is not None]
    if not known:
        return [now_ms - (count - 1 - index) * step for index in range(count)]

    out = [0] * count
    first = known[0]
    anchor = carried[first]
    assert anchor is not None
    out[first] = anchor
    previous = anchor
    for index in range(first + 1, count):
        want = carried[index]
        floor = previous + step
        chosen = floor if want is None or want < floor else want
        out[index] = chosen
        previous = chosen
    previous = anchor
    for index in range(first - 1, -1, -1):
        previous -= step
        out[index] = previous
    return out
