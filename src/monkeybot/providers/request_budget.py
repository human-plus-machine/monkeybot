"""Shared provider request-size budgets for multimodal messages."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Sequence

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import ContentBlock, File, Image, Text, ToolResponse
from monkeybot.core.types.interfaces import LLMError
from monkeybot.core.types.types_tools import ToolDef

_DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_REQUEST_OVERHEAD_BYTES = 4096
_MEDIA_STUB = Text(
    text=(
        "[media omitted to fit the provider request size; previously shown. "
        "Call load_file with the original path or attachment_id to reload.]"
    )
)
_log = logging.getLogger(__name__)


class RequestByteBudgetError(LLMError):
    """A provider request remains oversized after all media is removed."""


def configured_request_byte_budget(default: int = _DEFAULT_MAX_REQUEST_BYTES) -> int:
    """Read the generic model request budget, falling back to *default*."""
    from monkeybot.core.config.snapshot import current_env

    raw = current_env("MODEL_MAX_REQUEST_BYTES", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            _log.warning("invalid MODEL_MAX_REQUEST_BYTES %s", kv(value=raw, default=default))
    return default


def _block_bytes(block: ContentBlock) -> int:
    return len(
        json.dumps(block.to_dict(), ensure_ascii=False, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _request_bytes(messages: Sequence[Message], tools: Sequence[ToolDef]) -> int:
    payload = {
        "messages": [
            {"role": message.role, "content": [block.to_dict() for block in message.content]}
            for message in messages
        ],
        "tools": [tool.to_model_schema() for tool in tools],
    }
    return _REQUEST_OVERHEAD_BYTES + len(
        json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    )


def trim_message_media_for_byte_budget(
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    max_bytes: int | None = None,
    provider: str,
) -> list[Message]:
    """Stub oldest media until a conservative provider-neutral request estimate fits.

    *max_bytes* defaults to the configured ``MODEL_MAX_REQUEST_BYTES`` budget.
    """
    if max_bytes is None:
        max_bytes = configured_request_byte_budget()
    current = _request_bytes(messages, tools)
    if current <= max_bytes:
        return list(messages)

    contents = [list(message.content) for message in messages]
    locations: list[tuple[int, int, int | None]] = []
    for message_index, blocks in enumerate(contents):
        for block_index, block in enumerate(blocks):
            if isinstance(block, (Image, File)):
                locations.append((message_index, block_index, None))
            elif isinstance(block, ToolResponse):
                for nested_index, result_block in enumerate(block.result):
                    if isinstance(result_block, (Image, File)):
                        locations.append((message_index, block_index, nested_index))

    stub_bytes = _block_bytes(_MEDIA_STUB)
    stubbed = 0
    for message_index, block_index, location_result_index in locations:
        if current <= max_bytes:
            break
        block = contents[message_index][block_index]
        if location_result_index is None:
            current += stub_bytes - _block_bytes(block)
            contents[message_index][block_index] = _MEDIA_STUB
        elif isinstance(block, ToolResponse):
            result = list(block.result)
            current += stub_bytes - _block_bytes(result[location_result_index])
            result[location_result_index] = _MEDIA_STUB
            contents[message_index][block_index] = dataclasses.replace(block, result=result)
        stubbed += 1

    trimmed = [
        dataclasses.replace(message, content=contents[index])
        for index, message in enumerate(messages)
    ]
    if stubbed:
        _log.warning(
            "trimmed provider request media %s",
            kv(
                provider=provider,
                body_bytes_after=current,
                max_request_bytes=max_bytes,
                trimmed_media=stubbed,
            ),
        )
    if current > max_bytes:
        _log.error(
            "provider request remains over byte cap after media trim %s",
            kv(provider=provider, body_bytes=current, max_request_bytes=max_bytes),
        )
        raise RequestByteBudgetError(
            f"{provider} request exceeds configured byte cap after removing media "
            f"({current} > {max_bytes})"
        )
    return trimmed


__all__ = [
    "RequestByteBudgetError",
    "configured_request_byte_budget",
    "trim_message_media_for_byte_budget",
]
