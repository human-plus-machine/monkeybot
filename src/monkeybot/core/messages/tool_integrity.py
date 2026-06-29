"""In-memory repair for broken tool-call turns before provider replay.

Repairs are applied on every harness turn after ``history.load()`` and are never
persisted. Structured warnings are logged for Langfuse / log aggregation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Text,
    ToolRequest,
    ToolResponse,
)

logger = logging.getLogger(__name__)

_SYNTHETIC_MISSING_RESULT = "Tool call interrupted or result missing"
_UNKNOWN_TOOL = "unknown_tool"


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
    new_blocks: list[object] = []
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
    return Message(role=msg.role, content=[b for b in new_blocks if isinstance(b, ContentBlock)])


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
        result=[Text(text=_SYNTHETIC_MISSING_RESULT)],
        is_error=True,
    )


def _synthetic_tool_request(block: ToolResponse) -> ToolRequest:
    name = _tool_name_for_response(block, {})
    _log_repair(
        "synthetic_tool_request",
        call_id=block.id,
        tool_name=name,
    )
    return ToolRequest(id=block.id, name=name, args={})


def repair_tool_turn_integrity(messages: Sequence[Message]) -> list[Message]:
    """Return a copy of ``messages`` with tool-turn structural issues synthesized away."""
    if not messages:
        return []

    out: list[Message] = []
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.role == "assistant":
            repaired = _repair_assistant_message(msg)
            requests = _tool_requests(repaired)
            if requests:
                request_map = {r.id: r for r in requests}
                request_ids = set(request_map)

                if i + 1 < len(messages) and messages[i + 1].role == "user":
                    user_msg = messages[i + 1]
                    user_responses = _tool_responses(user_msg)
                    orphan_responses = [b for b in user_responses if b.id not in request_ids]

                    repaired_blocks: list[object] = []
                    user_changed = False
                    for block in user_msg.content:
                        if isinstance(block, ToolResponse):
                            fixed = _repair_tool_response(block, request_map)
                            if fixed is not block:
                                user_changed = True
                            repaired_blocks.append(fixed)
                        else:
                            repaired_blocks.append(block)

                    response_ids = {
                        b.id for b in repaired_blocks if isinstance(b, ToolResponse)
                    }
                    missing_ids = sorted(request_ids - response_ids)
                    if missing_ids:
                        for req_id in missing_ids:
                            repaired_blocks.append(_synthetic_error_response(request_map[req_id]))
                        user_changed = True

                    repaired_user = Message(
                        role="user",
                        content=[b for b in repaired_blocks if isinstance(b, ContentBlock)],
                    )
                    out.append(repaired)
                    if orphan_responses:
                        out.append(
                            Message(
                                role="assistant",
                                content=[
                                    _synthetic_tool_request(block) for block in orphan_responses
                                ],
                            )
                        )
                    out.append(repaired_user if user_changed else user_msg)
                    i += 2
                    continue

                out.append(repaired)
                out.append(
                    Message(
                        role="user",
                        content=[_synthetic_error_response(r) for r in requests],
                    )
                )
                i += 1
                continue

            out.append(repaired)
            i += 1
            continue

        if msg.role == "user":
            user_responses = _tool_responses(msg)
            if user_responses:
                prior_requests = {
                    b.id: b
                    for m in reversed(out)
                    if m.role == "assistant"
                    for b in m.content
                    if isinstance(b, ToolRequest)
                }
                orphan_responses = [b for b in user_responses if b.id not in prior_requests]
                if orphan_responses:
                    out.append(
                        Message(
                            role="assistant",
                            content=[
                                _synthetic_tool_request(block) for block in orphan_responses
                            ],
                        )
                    )

                user_blocks: list[object] = []
                changed = False
                for block in msg.content:
                    if isinstance(block, ToolResponse):
                        fixed = _repair_tool_response(block, prior_requests)
                        if fixed is not block:
                            changed = True
                        user_blocks.append(fixed)
                    else:
                        user_blocks.append(block)

                out.append(
                    Message(
                        role="user",
                        content=[b for b in user_blocks if isinstance(b, ContentBlock)],
                    )
                    if changed
                    else msg
                )
                i += 1
                continue

        out.append(msg)
        i += 1

    return out


__all__ = ["repair_tool_turn_integrity"]
