"""Shared provider utilities (cost estimation, Anthropic message shaping)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from monkeybot.core.provider import Message


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. ``pricing`` values are (input_$/M, output_$/M)."""
    rates = pricing.get(model, (0.0, 0.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def _parse_tool_placeholder(content: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Split assistant content into optional pre-text and ``tool_calls`` list if present."""
    segments: list[tuple[str, str]] = []
    last_nl = content.rfind("\n")
    if last_nl >= 0:
        segments.append((content[:last_nl], content[last_nl + 1 :]))
    segments.append(("", content))
    for pre_text, json_part in segments:
        try:
            parsed = json.loads(json_part)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        raw_calls = parsed.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            continue
        calls: list[dict[str, Any]] = []
        for c in raw_calls:
            if not isinstance(c, dict) or "call_id" not in c or "name" not in c:
                calls = []
                break
            calls.append(c)
        if calls:
            return pre_text, calls
    return content, None


def build_anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert harness :class:`Message` rows to Anthropic ``messages`` API shape."""
    result: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            pre_text, tool_calls = _parse_tool_placeholder(m.content)
            if tool_calls is not None:
                blocks: list[dict[str, Any]] = []
                if pre_text:
                    blocks.append({"type": "text", "text": pre_text})
                for tc in tool_calls:
                    args = tc.get("args", {})
                    if not isinstance(args, dict):
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": str(tc["call_id"]),
                            "name": str(tc["name"]),
                            "input": args,
                        }
                    )
                result.append({"role": "assistant", "content": blocks})
            else:
                result.append({"role": "assistant", "content": m.content})
        else:
            result.append({"role": "user", "content": m.content})
    return result


__all__ = ["build_anthropic_messages", "estimate_cost", "_parse_tool_placeholder"]
