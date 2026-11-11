"""Store a repeated ``read_file`` result once.

The ledger is a hint. A stub is stored only when the earlier result is still in
this thread and its text still matches. Anything else — a different thread, a
cleared ledger, a missing or trimmed row — stores the full text again.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from monkeybot.core.context.common import text_from_blocks
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.tools.read_ledger import FileReadLedger
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse

logger = logging.getLogger(__name__)

# Shorter results cost less than a history load plus a stub the model must interpret.
READ_REPEAT_MIN_CHARS = 2000

STUB_NOTE = (
    "Same text as your earlier read_file result for this path and line range, "
    "so it is not repeated. If that result is no longer visible to you, "
    "call read_file again and the text will be sent."
)


@dataclass(frozen=True)
class _ReadIdentity:
    path: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    next_offset: int | None


@dataclass(frozen=True)
class _ReadDecision:
    """What to record after the batch is stored, for one ``read_file`` result."""

    call_id: str
    thread_id: str
    path: str
    start_line: int
    end_line: int
    original_text: str
    stub_text: str | None


def text_hash(text: str) -> str:
    """Stable hash of a stored tool result. The text itself is not kept."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_int(value: object) -> int | None:
    if type(value) is int:
        return value
    return None


def _read_identity(text: str) -> _ReadIdentity | None:
    """Path and line range of a successful ``read_file`` body, or ``None``."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    if payload.get("unchanged") is True or not isinstance(payload.get("content"), str):
        return None
    path = payload.get("path")
    start = _as_int(payload.get("start_line"))
    end = _as_int(payload.get("end_line"))
    total = _as_int(payload.get("total_lines"))
    if not isinstance(path, str) or not path or start is None or end is None or total is None:
        return None
    truncated = payload.get("truncated") is True
    next_offset = _as_int(payload.get("next_offset"))
    return _ReadIdentity(path, start, end, total, truncated, next_offset)


def _stub_text(identity: _ReadIdentity) -> str:
    stub: dict[str, Any] = {
        "ok": True,
        "path": identity.path,
        "unchanged": True,
        "start_line": identity.start_line,
        "end_line": identity.end_line,
        "total_lines": identity.total_lines,
        "note": STUB_NOTE,
    }
    if identity.truncated:
        stub["truncated"] = True
    if identity.next_offset is not None:
        stub["next_offset"] = identity.next_offset
    return json.dumps(stub, ensure_ascii=False)


def _response_text(block: ToolResponse) -> str:
    return text_from_blocks(list(block.result))


async def _stored_texts(
    history: HistoryStore, thread_id: str, call_ids: set[str]
) -> dict[str, str] | None:
    """Text of the named tool responses, or ``None`` when history cannot be read."""
    try:
        rows = await history.load(thread_id)
    except Exception:
        logger.warning(
            "repeat-read check could not load history %s",
            kv(thread_id=thread_id),
            exc_info=True,
        )
        return None
    found: dict[str, str] = {}
    for row in rows:
        for block in row.content:
            if isinstance(block, ToolResponse) and not block.is_error and block.id in call_ids:
                found[block.id] = _response_text(block)
    return found


async def replace_repeated_reads(
    blocks: Sequence[ContentBlock],
    *,
    history: HistoryStore,
    thread_id: str,
    ledger: FileReadLedger | None,
) -> tuple[list[ContentBlock], list[_ReadDecision]]:
    """Replace a confirmed repeat with a stub. The first read of a range is unchanged.

    History is loaded only when a ledger hint says this exact text was stored before.
    """
    prepared = list(blocks)
    if ledger is None:
        return prepared, []

    candidates: list[tuple[int, ToolResponse, _ReadIdentity, str]] = []
    for index, block in enumerate(prepared):
        if not isinstance(block, ToolResponse) or block.is_error or block.tool_name != "read_file":
            continue
        text = _response_text(block)
        if len(text) < READ_REPEAT_MIN_CHARS or not block.id:
            continue
        identity = _read_identity(text)
        if identity is None:
            continue
        shown = ledger.shown_read(thread_id, identity.path, identity.start_line, identity.end_line)
        if shown is None or shown.stubbed or shown.text_hash != text_hash(text):
            continue
        candidates.append((index, block, identity, shown.call_id))

    confirmed: dict[str, str] | None = {}
    if candidates:
        confirmed = await _stored_texts(history, thread_id, {item[3] for item in candidates})

    decisions: list[_ReadDecision] = []
    for index, block, identity, prior_id in candidates:
        text = _response_text(block)
        stored = None if confirmed is None else confirmed.get(prior_id)
        if stored is None or text_hash(stored) != text_hash(text):
            if confirmed is not None:
                ledger.forget_shown_read(
                    thread_id, identity.path, identity.start_line, identity.end_line
                )
            continue
        stub = _stub_text(identity)
        prepared[index] = ToolResponse(
            id=block.id,
            tool_name=block.tool_name,
            result=[Text(text=stub)],
            is_error=False,
        )
        decisions.append(
            _ReadDecision(
                call_id=block.id,
                thread_id=thread_id,
                path=identity.path,
                start_line=identity.start_line,
                end_line=identity.end_line,
                original_text=text,
                stub_text=stub,
            )
        )
        logger.info(
            "read result not repeated %s",
            kv(
                thread_id=thread_id,
                path=identity.path,
                call_id=block.id,
                prior_call_id=prior_id,
            ),
        )

    for block in prepared:
        if not isinstance(block, ToolResponse) or block.is_error or block.tool_name != "read_file":
            continue
        if any(
            decision.call_id == block.id and decision.stub_text is not None
            for decision in decisions
        ):
            continue
        text = _response_text(block)
        identity = _read_identity(text)
        if identity is None or len(text) < READ_REPEAT_MIN_CHARS or not block.id:
            continue
        decisions.append(
            _ReadDecision(
                call_id=block.id,
                thread_id=thread_id,
                path=identity.path,
                start_line=identity.start_line,
                end_line=identity.end_line,
                original_text=text,
                stub_text=None,
            )
        )
    return prepared, decisions


def remember_stored_reads(
    stored: Sequence[ContentBlock],
    decisions: Sequence[_ReadDecision],
    ledger: FileReadLedger | None,
) -> None:
    """Record a read only when the stored text is the text the tool returned.

    A stub marks the existing hint so the next read of that range is sent in full.
    A trimmed result is not recorded, and a later read sends the file again.
    """
    if ledger is None or not decisions:
        return
    by_id: dict[str, str] = {}
    for block in stored:
        if (
            isinstance(block, ToolResponse)
            and block.tool_name == "read_file"
            and not block.is_error
        ):
            by_id[block.id] = _response_text(block)
    for decision in decisions:
        kept = by_id.get(decision.call_id)
        if kept is None:
            continue
        if decision.stub_text is not None:
            if kept == decision.stub_text:
                ledger.mark_shown_stubbed(
                    decision.thread_id,
                    decision.path,
                    decision.start_line,
                    decision.end_line,
                )
            continue
        if kept != decision.original_text:
            continue
        ledger.record_shown_read(
            decision.thread_id,
            decision.path,
            decision.start_line,
            decision.end_line,
            call_id=decision.call_id,
            text_hash=text_hash(kept),
            stubbed=False,
        )
