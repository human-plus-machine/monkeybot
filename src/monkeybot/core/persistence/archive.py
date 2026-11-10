"""Rows removed from a live thread by compaction, kept until the chat is deleted."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import cast

from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.persistence.backends import ArchivedRow
from monkeybot.core.types.content_blocks import ContentBlock


def unmatched_indexes(stored_ids: Sequence[str], kept_ids: Sequence[str]) -> list[int]:
    """Indexes in ``stored_ids`` that ``kept_ids`` does not account for.

    Matching is by count. One kept copy of an id leaves a second stored copy
    unmatched, and a kept id that was never stored is ignored.
    """
    remaining = Counter(kept_ids)
    dropped: list[int] = []
    for index, row_id in enumerate(stored_ids):
        if remaining[row_id] > 0:
            remaining[row_id] -= 1
            continue
        dropped.append(index)
    return dropped


def kept_row_ids(messages: Sequence[Message]) -> list[str]:
    """Row ids the replacement still contains. A new row has none yet."""
    return [message.row_id for message in messages if message.row_id]


def archived_row(
    *,
    role: str,
    content_blob: str,
    row_id: str,
    created_at: int,
    turn_id: str | None,
    message_id: str | None,
    archive_id: str,
    summary_row_id: str | None,
    reason: str,
    position: int,
    archived_at: int,
) -> ArchivedRow:
    """One archived row from the JSON stored for it. Raises if that JSON is unusable."""
    raw = json.loads(content_blob)
    if not isinstance(raw, list):
        raise ValueError("stored content must be a JSON array")
    blocks = [ContentBlock.from_dict(block) for block in raw]
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"invalid role: {role!r}")
    return ArchivedRow(
        message=Message(role=cast(Role, role), content=blocks, row_id=row_id),
        created_at=created_at,
        turn_id=turn_id,
        message_id=message_id,
        archive_id=archive_id,
        summary_row_id=summary_row_id,
        reason=reason,
        position=position,
        archived_at=archived_at,
    )
