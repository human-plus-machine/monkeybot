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

# Conservative transport defaults for providers that do not expose a model-specific
# request limit. MODEL_MAX_REQUEST_BYTES remains the authoritative override.
_DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_PROVIDER_DEFAULT_MAX_REQUEST_BYTES = {
    "openai": 32 * 1024 * 1024,
    "claude": 32 * 1024 * 1024,
    "vertex-claude": 32 * 1024 * 1024,
    "bedrock": 25 * 1024 * 1024,
    "gemini": 20 * 1024 * 1024,
}
# Covers top-level provider fields that are added after the neutral message/tool
# representation is converted to a provider-specific request.
_REQUEST_OVERHEAD_BYTES = 4096
_log = logging.getLogger(__name__)


class RequestByteBudgetError(LLMError):
    """A provider request remains oversized after all media is removed."""


def configured_request_byte_budget(
    default: int | None = None,
    *,
    provider: str | None = None,
) -> int:
    """Read the generic model request budget.

    Falls back to *default* when set, otherwise to *provider*'s transport limit
    (``_DEFAULT_MAX_REQUEST_BYTES`` for providers without a known one).
    """
    from monkeybot.core.config.snapshot import current_env

    if default is None:
        default = _PROVIDER_DEFAULT_MAX_REQUEST_BYTES.get(
            provider or "", _DEFAULT_MAX_REQUEST_BYTES
        )
    raw = current_env("MODEL_MAX_REQUEST_BYTES", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            _log.warning("invalid MODEL_MAX_REQUEST_BYTES %s", kv(value=raw, default=default))
    return default


def _metadata_str(block: Image | File, *keys: str) -> str | None:
    metadata = block.metadata or {}
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _media_stub(block: Image | File) -> Text:
    attachment_id = _metadata_str(block, "attachment_id", "attachmentId")
    path = _metadata_str(block, "path")
    filename = _metadata_str(block, "original_filename", "filename", "name")
    if attachment_id:
        recovery = f'Call load_file(attachment_id="{attachment_id}") to reload it.'
    elif path:
        recovery = f'Call load_file(path="{path}") to reload it.'
    elif filename:
        recovery = f"Ask the user to reattach {filename} if it is needed again."
    else:
        recovery = "Ask the user to reattach it if it is needed again."
    return Text(
        text=f"[media omitted to fit the provider request size; previously shown. {recovery}]"
    )


def _block_bytes(block: ContentBlock) -> int:
    if isinstance(block, (Image, File)):
        # Base64 is ASCII without JSON escapes; serialize only the small envelope.
        payload = block.to_dict()
        data = str(payload.pop("data"))
        payload["data"] = ""
        return len(data) + _json_bytes(payload)
    if isinstance(block, ToolResponse):
        payload = block.to_dict()
        payload["result"] = []
        return (
            _json_bytes(payload)
            - 2
            + _json_list_bytes([_block_bytes(item) for item in block.result])
        )
    return _json_bytes(block.to_dict())


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    )


def _json_list_bytes(item_sizes: Sequence[int]) -> int:
    return 2 + sum(item_sizes) + max(0, len(item_sizes) - 1)


def _request_bytes(messages: Sequence[Message], tools: Sequence[ToolDef]) -> int:
    message_sizes = []
    for message in messages:
        envelope = _json_bytes({"role": message.role, "content": []})
        content_size = _json_list_bytes([_block_bytes(block) for block in message.content])
        message_sizes.append(envelope - 2 + content_size)
    tool_sizes = [_json_bytes(tool.to_model_schema()) for tool in tools]
    empty_payload = _json_bytes({"messages": [], "tools": []})
    return (
        _REQUEST_OVERHEAD_BYTES
        + empty_payload
        - 4
        + _json_list_bytes(message_sizes)
        + _json_list_bytes(tool_sizes)
    )


def trim_message_media_for_byte_budget(
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    max_bytes: int | None = None,
    provider: str,
    raise_if_oversized: bool = True,
) -> list[Message]:
    """Stub oldest media until a conservative provider-neutral request estimate fits.

    *max_bytes* defaults to the configured ``MODEL_MAX_REQUEST_BYTES`` budget.

    With *raise_if_oversized* (the default) an estimate still over the cap once
    every media block is stubbed raises ``RequestByteBudgetError``, so send paths
    fail closed instead of shipping a request the transport will reject. Token
    counting passes False: an over-cap text-only history is exactly what the
    caller is measuring to decide whether to compact, so it must not raise.
    """
    if max_bytes is None:
        max_bytes = configured_request_byte_budget(provider=provider)
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

    stubbed = 0
    for message_index, block_index, location_result_index in locations:
        if current <= max_bytes:
            break
        block = contents[message_index][block_index]
        if location_result_index is None:
            if not isinstance(block, (Image, File)):
                continue
            stub = _media_stub(block)
            current += _block_bytes(stub) - _block_bytes(block)
            contents[message_index][block_index] = stub
        elif isinstance(block, ToolResponse):
            result = list(block.result)
            media = result[location_result_index]
            if not isinstance(media, (Image, File)):
                continue
            stub = _media_stub(media)
            current += _block_bytes(stub) - _block_bytes(media)
            result[location_result_index] = stub
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
    if current > max_bytes and raise_if_oversized:
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
