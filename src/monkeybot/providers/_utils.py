"""Shared provider utilities (cost estimation, Anthropic message shaping)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Image,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.llm.provider import Message


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. ``pricing`` values are (input_$/M, output_$/M)."""
    rates = pricing.get(model, (0.0, 0.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def _anthropic_tool_result_content(result: list[ContentBlock]) -> list[dict[str, Any]]:
    """Map ``ToolResponse.result`` blocks to Anthropic ``tool_result`` content list."""
    out: list[dict[str, Any]] = []
    for block in result:
        if isinstance(block, Text):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type,
                        "data": block.data,
                    },
                }
            )
        else:
            raise ValueError(
                f"unsupported ToolResponse block for Anthropic: {type(block).__name__}"
            )
    return out


def _anthropic_user_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, Image):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.mime_type,
                "data": block.data,
            },
        }
    if isinstance(block, ToolResponse):
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": _anthropic_tool_result_content(block.result),
        }
    raise ValueError(f"unsupported user content block for Anthropic: {type(block).__name__}")


def _anthropic_assistant_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolRequest):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.args),
        }
    if isinstance(block, Thinking):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    if isinstance(block, RedactedThinking):
        return {"type": "redacted_thinking", "data": block.data}
    raise ValueError(
        f"unsupported assistant content block for Anthropic: {type(block).__name__}"
    )


def build_anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert harness messages to Anthropic ``messages`` API shape (no string parsing)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role == "tool":
            raise ValueError('disallowed role "tool"; use user messages with ToolResponse blocks')
        if role not in ("user", "assistant"):
            raise ValueError(f"unsupported role for Anthropic messages: {role!r}")

        blocks = list(m.content)

        if role == "user":
            if len(blocks) == 1 and isinstance(blocks[0], Text):
                out.append({"role": "user", "content": blocks[0].text})
                continue
            out.append(
                {
                    "role": "user",
                    "content": [_anthropic_user_block(b) for b in blocks],
                }
            )
            continue

        # assistant
        if len(blocks) == 1 and isinstance(blocks[0], Text):
            out.append({"role": "assistant", "content": blocks[0].text})
            continue
        out.append(
            {
                "role": "assistant",
                "content": [_anthropic_assistant_block(b) for b in blocks],
            }
        )
    return out


def estimate_anthropic_input_tokens(
    *,
    system: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
) -> int:
    """Rough prompt token estimate when provider ``count_tokens`` is unavailable (e.g. Bedrock)."""
    parts: list[str] = [system]
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(json.dumps(content, ensure_ascii=False))
        else:
            parts.append(str(content))
    if tools:
        parts.append(json.dumps(list(tools), ensure_ascii=False))
    char_count = sum(len(p) for p in parts)
    return max(1, char_count // 4)


__all__ = ["build_anthropic_messages", "estimate_anthropic_input_tokens", "estimate_cost"]
