"""In-memory repair for broken tool-call turns before provider replay.

Repairs are applied on every harness turn after ``history.load()`` and are never
persisted. Structured warnings are logged for Langfuse / log aggregation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Text,
    ToolRequest,
    ToolResponse,
)

logger = logging.getLogger(__name__)

_UNKNOWN_TOOL = "unknown_tool"

MISSING_TOOL_RESULT_RECOVERY_HINT = (
    "Inspect the workspace and any spill/partial outputs before re-issuing; "
    "do not assume the tool never ran."
)


def cancelled_tool_result_text(tool_name: str) -> str:
    return (
        f'Tool "{tool_name}" was cancelled before a result was recorded. '
        f"{MISSING_TOOL_RESULT_RECOVERY_HINT}"
    )


def interrupted_tool_result_text(tool_name: str) -> str:
    return (
        f'Tool "{tool_name}" was interrupted or its result is missing. '
        f"{MISSING_TOOL_RESULT_RECOVERY_HINT}"
    )


def _log_repair(action: str, **fields: object) -> None:
    logger.warning("tool_integrity_repair %s", kv(action=action, **fields))


def _tool_responses(msg: Message) -> list[ToolResponse]:
    return [b for b in msg.content if isinstance(b, ToolResponse)]


def _tool_requests(msg: Message) -> list[ToolRequest]:
    return [b for b in msg.content if isinstance(b, ToolRequest)]


def _repair_tool_request(block: ToolRequest) -> ToolRequest:
    args = block.args
    if isinstance(args, dict):
        return block
    _log_repair(
        "malformed_stored_args",
        call_id=block.id,
        tool_name=block.name,
    )
    return ToolRequest(
        id=block.id,
        name=block.name,
        args={},
        parse_error=block.parse_error,
        metadata=block.metadata,
    )


def _repair_assistant_message(msg: Message) -> Message:
    new_blocks: list[ContentBlock] = []
    changed = False
    for block in msg.content:
        if isinstance(block, ToolRequest):
            fixed = _repair_tool_request(block)
            if fixed is not block:
                changed = True
            new_blocks.append(fixed)
        else:
            new_blocks.append(block)
    if not changed:
        return msg
    return replace(msg, content=new_blocks)


def _tool_name_for_response(
    block: ToolResponse,
    request_by_id: dict[str, ToolRequest],
) -> str:
    name = (block.tool_name or "").strip()
    if name:
        return name
    req = request_by_id.get(block.id)
    if req is not None and req.name.strip():
        return req.name
    return _UNKNOWN_TOOL


def _repair_tool_response(
    block: ToolResponse,
    request_by_id: dict[str, ToolRequest],
) -> ToolResponse:
    tool_name = _tool_name_for_response(block, request_by_id)
    if tool_name == block.tool_name:
        return block
    _log_repair(
        "empty_tool_name",
        call_id=block.id,
        tool_name=tool_name,
    )
    return ToolResponse(
        id=block.id,
        tool_name=tool_name,
        result=list(block.result),
        is_error=block.is_error,
    )


def _synthetic_error_response(req: ToolRequest) -> ToolResponse:
    _log_repair(
        "synthetic_tool_result",
        call_id=req.id,
        tool_name=req.name,
    )
    return ToolResponse(
        id=req.id,
        tool_name=req.name,
        result=[Text(text=interrupted_tool_result_text(req.name))],
        is_error=True,
    )


def _synthetic_tool_request(
    block: ToolResponse,
    request_by_id: dict[str, ToolRequest],
) -> ToolRequest | None:
    name = _tool_name_for_response(block, request_by_id)
    if name == _UNKNOWN_TOOL:
        _log_repair(
            "skip_synthetic_tool_request",
            call_id=block.id,
            reason="unresolvable_tool_name",
        )
        return None
    _log_repair(
        "synthetic_tool_request",
        call_id=block.id,
        tool_name=name,
    )
    return ToolRequest(id=block.id, name=name, args={})


def _synthetic_tool_requests(
    blocks: Sequence[ToolResponse],
    request_by_id: dict[str, ToolRequest],
) -> list[ToolRequest]:
    out: list[ToolRequest] = []
    for block in blocks:
        req = _synthetic_tool_request(block, request_by_id)
        if req is not None:
            out.append(req)
    return out


def _append_synthetic_requests(out: list[Message], requests: Sequence[ToolRequest]) -> None:
    if not requests:
        return
    out.append(Message(role="assistant", content=cast(list[ContentBlock], list(requests))))


def _settle_user_message(
    user_msg: Message,
    *,
    request_by_id: dict[str, ToolRequest],
    answered: set[str],
) -> tuple[Message | None, list[ToolResponse]]:
    """Pair ``user_msg`` with the immediately previous assistant's tool requests.

    A response matches only when its id is in ``request_by_id``. A repeated id in
    this message is dropped. A response whose id was already answered by an
    earlier turn, and is not one of these requests, is dropped. Anything else
    is an orphan and is returned so the caller can synthesize a request.

    Missing request ids get a synthetic error result. Returns ``None`` when the
    message would be empty.
    """
    kept: list[ContentBlock] = []
    orphans: list[ToolResponse] = []
    seen: set[str] = set()
    changed = False
    request_ids = set(request_by_id)

    for block in user_msg.content:
        if not isinstance(block, ToolResponse):
            kept.append(block)
            continue
        duplicate = block.id in seen or (block.id in answered and block.id not in request_ids)
        if duplicate:
            changed = True
            _log_repair(
                "drop_duplicate_tool_result",
                call_id=block.id,
                tool_name=block.tool_name,
            )
            continue
        fixed = _repair_tool_response(block, request_by_id)
        if fixed is not block:
            changed = True
        if block.id not in request_ids:
            orphans.append(fixed)
        seen.add(block.id)
        kept.append(fixed)

    for req_id in sorted(request_ids - seen):
        kept.append(_synthetic_error_response(request_by_id[req_id]))
        seen.add(req_id)
        changed = True

    answered.update(seen)
    if not kept:
        return None, []
    return (replace(user_msg, content=kept) if changed else user_msg), orphans


def repair_tool_turn_integrity(messages: Sequence[Message]) -> list[Message]:
    """Return a copy of ``messages`` with tool-turn structural issues synthesized away.

    A tool result pairs only with a request in the immediately previous assistant
    message. A second result for an id that already has one is dropped.
    """
    if not messages:
        return []

    out: list[Message] = []
    answered: set[str] = set()
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.role == "assistant":
            repaired = _repair_assistant_message(msg)
            requests = _tool_requests(repaired)
            if requests:
                request_map = {r.id: r for r in requests}

                if i + 1 < len(messages) and messages[i + 1].role == "user":
                    repaired_user, orphan_responses = _settle_user_message(
                        messages[i + 1],
                        request_by_id=request_map,
                        answered=answered,
                    )
                    out.append(repaired)
                    _append_synthetic_requests(
                        out, _synthetic_tool_requests(orphan_responses, request_map)
                    )
                    if repaired_user is not None:
                        out.append(repaired_user)
                    i += 2
                    continue

                out.append(repaired)
                out.append(
                    Message(
                        role="user",
                        content=[_synthetic_error_response(r) for r in requests],
                    )
                )
                answered.update(request_map)
                i += 1
                continue

            out.append(repaired)
            i += 1
            continue

        if msg.role == "user" and _tool_responses(msg):
            previous = out[-1] if out else None
            request_map = (
                {b.id: b for b in _tool_requests(previous)}
                if previous is not None and previous.role == "assistant"
                else {}
            )
            repaired_user, orphan_responses = _settle_user_message(
                msg,
                request_by_id=request_map,
                answered=answered,
            )
            _append_synthetic_requests(out, _synthetic_tool_requests(orphan_responses, request_map))
            if repaired_user is not None:
                out.append(repaired_user)
            i += 1
            continue

        out.append(msg)
        i += 1

    return out


__all__ = ["repair_tool_turn_integrity"]
