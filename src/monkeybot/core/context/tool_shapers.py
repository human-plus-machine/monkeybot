"""Content-aware shaping for large tool outputs (no ML, pure Python)."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from typing import Any, Literal

from monkeybot.core.context.common import ContextPressureTier, text_from_blocks
from monkeybot.core.context.tool_output_policy import ToolOutputBudget, resolve_tool_budget
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse

logger = logging.getLogger(__name__)

ContentType = Literal["json", "logs", "code", "prose"]

_DEFAULT_LOG_HEAD_LINES = 40
_DEFAULT_LOG_TAIL_LINES = 40
_DEFAULT_MAX_ARRAY_ITEMS = 50
_DEFAULT_ERROR_PATTERNS = (
    r"(?i)\berror\b",
    r"(?i)\bfatal\b",
    r"(?i)traceback",
    r"(?i)\bexception\b",
    r"(?i)\bfailed\b",
)
_CODE_MARKERS = re.compile(
    r"(^\s*(def |class |import |from .+ import |function |const |let |var |#include ))",
    re.MULTILINE,
)


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "invalid env var value %s",
            kv(name=name, value=raw, default=default),
        )
        return default


def log_head_lines_from_env() -> int:
    return _int_from_env("MONKEYBOT_SHAPE_LOG_HEAD_LINES", _DEFAULT_LOG_HEAD_LINES)


def log_tail_lines_from_env() -> int:
    return _int_from_env("MONKEYBOT_SHAPE_LOG_TAIL_LINES", _DEFAULT_LOG_TAIL_LINES)


def max_array_items_from_env() -> int:
    return _int_from_env("MONKEYBOT_SHAPE_MAX_ARRAY_ITEMS", _DEFAULT_MAX_ARRAY_ITEMS)


def classify_content(text: str, *, tool_name: str, hint: str | None = None) -> ContentType:
    """Route tool text to a shaper category."""
    if hint and hint != "auto":
        return hint  # type: ignore[return-value]

    stripped = text.lstrip()
    if tool_name in ("run_command",):
        return "logs"
    if tool_name in ("web_search", "search_memory", "task") and stripped.startswith(("{", "[")):
        return "json"
    if tool_name in ("read_file", "write_file", "replace_in_file", "glob"):
        return "code"

    if stripped.startswith(("{", "[")):
        try:
            json.loads(text)
            return "json"
        except json.JSONDecodeError:
            pass

    lines = text.splitlines()
    if len(lines) >= 8:
        duplicate_run = sum(
            1 for i in range(1, len(lines)) if lines[i] == lines[i - 1] and lines[i].strip()
        )
        if duplicate_run >= max(3, len(lines) // 10):
            return "logs"
        if any(_CODE_MARKERS.search(line) for line in lines[:20]):
            return "code"

    if len(lines) >= 30 and not stripped.startswith("{"):
        return "logs"

    return "prose"


def _compiled_keep_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    src = patterns or _DEFAULT_ERROR_PATTERNS
    out: list[re.Pattern[str]] = []
    for pat in src:
        try:
            out.append(re.compile(pat))
        except re.error:
            continue
    return tuple(out)


def _line_matches_keep(line: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(rx.search(line) for rx in patterns)


def shape_logs(
    text: str,
    *,
    max_lines: int | None,
    keep_patterns: tuple[str, ...],
    collapse_repeated: bool,
) -> str:
    lines = text.splitlines()
    if len(lines) <= 1:
        return text

    compiled = _compiled_keep_patterns(keep_patterns)
    if collapse_repeated:
        collapsed: list[str] = []
        prev: str | None = None
        repeat_count = 0
        for line in lines:
            if line == prev and line.strip():
                repeat_count += 1
                continue
            if repeat_count > 0 and prev is not None:
                collapsed.append(f"{prev}  … (repeated {repeat_count + 1}×)")
                repeat_count = 0
            if prev is not None:
                collapsed.append(prev)
            prev = line
        if repeat_count > 0 and prev is not None:
            collapsed.append(f"{prev}  … (repeated {repeat_count + 1}×)")
        elif prev is not None:
            collapsed.append(prev)
        lines = collapsed

    cap = max_lines
    if cap is None or len(lines) <= cap:
        return "\n".join(lines)

    head_n = min(log_head_lines_from_env(), max(1, cap // 2))
    tail_n = min(log_tail_lines_from_env(), max(1, cap - head_n))
    keep_indices = set(range(min(head_n, len(lines))))
    keep_indices.update(range(max(0, len(lines) - tail_n), len(lines)))
    for i, line in enumerate(lines):
        if _line_matches_keep(line, compiled):
            keep_indices.add(i)

    if len(keep_indices) >= cap:
        ordered = sorted(keep_indices)
        selected = [lines[i] for i in ordered[:cap]]
        omitted = len(lines) - len(selected)
        if omitted > 0:
            selected.append(f"… (+{omitted} more lines omitted by log shaper)")
        return "\n".join(selected)

    ordered = sorted(keep_indices)
    selected = [lines[i] for i in ordered]
    omitted = len(lines) - len(ordered)
    if omitted > 0:
        insert_at = len(selected)
        for i in range(len(lines)):
            if i not in keep_indices:
                insert_at = min(insert_at, len(selected))
                break
        marker = f"… (+{omitted} lines omitted by log shaper)"
        mid = len(selected) // 2
        selected = [*selected[:mid], marker, *selected[mid:]]
    return "\n".join(selected)


def _json_dedup_key(item: Any) -> str:
    try:
        return json.dumps(item, sort_keys=True, default=str)
    except TypeError:
        return repr(item)


def _json_row_is_anomaly(item: Any) -> bool:
    if isinstance(item, dict):
        blob = json.dumps(item, sort_keys=True).lower()
        return any(k in blob for k in ("error", "exception", "fail", "fatal"))
    if isinstance(item, str):
        lower = item.lower()
        return any(k in lower for k in ("error", "exception", "fail", "fatal"))
    return False


def _shape_json_value(value: Any, *, max_array_items: int, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, list):
        seen: set[str] = set()
        kept: list[Any] = []
        anomalies: list[Any] = []
        for item in value:
            if _json_row_is_anomaly(item):
                anomalies.append(_shape_json_value(item, max_array_items=max_array_items, depth=depth + 1))
                continue
            key = _json_dedup_key(item)
            if key in seen:
                continue
            seen.add(key)
            kept.append(_shape_json_value(item, max_array_items=max_array_items, depth=depth + 1))
        merged = anomalies + kept
        if len(merged) > max_array_items:
            trimmed = merged[:max_array_items]
            trimmed.append(f"… (+{len(merged) - max_array_items} more items)")
            return trimmed
        return merged
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[str(k)] = _shape_json_value(v, max_array_items=max_array_items, depth=depth + 1)
        return out
    return value


def shape_json_value(value: Any, *, max_array_items: int | None) -> Any:
    """Shape an already-parsed JSON value (no re-parse)."""
    cap = max_array_items if max_array_items is not None else max_array_items_from_env()
    return _shape_json_value(value, max_array_items=cap)


def shape_json(text: str, *, max_array_items: int | None) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    shaped = shape_json_value(parsed, max_array_items=max_array_items)
    return json.dumps(shaped, indent=2, ensure_ascii=False, default=str)


def exceeds_tool_output_budget(text: str, *, tool_name: str, budget: ToolOutputBudget) -> bool:
    """True when source text exceeds configured per-tool caps (safe to shape at append)."""
    if not text.strip():
        return False
    lines = text.splitlines()
    if budget.max_output_lines is not None and len(lines) > budget.max_output_lines:
        return True
    if budget.max_array_items is not None and tool_name in ("web_search", "search_memory", "task"):
        stripped = text.lstrip()
        if stripped.startswith(("[", "{")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return False
            if isinstance(parsed, list) and len(parsed) > budget.max_array_items:
                return True
    if budget.collapse_repeated and len(lines) > 30:
        repeats = sum(
            1 for i in range(1, len(lines)) if lines[i] == lines[i - 1] and lines[i].strip()
        )
        if repeats >= 5:
            return True
    return False


def shape_tool_text(
    text: str,
    *,
    tool_name: str,
    budget: ToolOutputBudget | None,
    pressure_tier: ContextPressureTier | None = None,
) -> str:
    """Apply content-aware shaping when policy or context pressure warrants it."""
    if not text or not text.strip():
        return text

    has_policy = budget is not None
    pressure_shape = pressure_tier in ("moderate", "aggressive")
    if not has_policy and not pressure_shape:
        return text

    hint = budget.content_type if budget is not None else None
    content_type = classify_content(text, tool_name=tool_name, hint=hint)

    if content_type == "code":
        return text
    if content_type == "prose" and not pressure_shape:
        return text

    if content_type == "json":
        cap = budget.max_array_items if budget is not None else None
        return shape_json(text, max_array_items=cap)

    if content_type == "logs":
        return shape_logs(
            text,
            max_lines=budget.max_output_lines if budget is not None else None,
            keep_patterns=budget.keep_patterns if budget is not None else (),
            collapse_repeated=bool(budget and budget.collapse_repeated),
        )

    return text


def shape_messages_tool_results(
    messages: Sequence[Message],
    *,
    protect_recent: int,
    pressure_tier: ContextPressureTier | None,
) -> list[Message]:
    """Shape tool results in older history rows when context pressure is elevated.

    The most recent ``protect_recent`` messages are left untouched so the model
    always sees fresh tool output verbatim.
    """
    if pressure_tier not in ("moderate", "aggressive"):
        return list(messages)
    cutoff = max(0, len(messages) - protect_recent)
    out: list[Message] = []
    for i, msg in enumerate(messages):
        if i >= cutoff or msg.role != "user":
            out.append(msg)
            continue
        new_content: list[ContentBlock] = []
        changed = False
        for block in msg.content:
            if not isinstance(block, ToolResponse) or block.is_error:
                new_content.append(block)
                continue
            text = text_from_blocks(list(block.result))
            budget = resolve_tool_budget(block.tool_name)
            shaped = shape_tool_text(
                text,
                tool_name=block.tool_name,
                budget=budget,
                pressure_tier=pressure_tier,
            )
            if shaped == text:
                new_content.append(block)
                continue
            changed = True
            new_content.append(
                ToolResponse(
                    id=block.id,
                    tool_name=block.tool_name,
                    result=[Text(text=shaped)],
                    is_error=block.is_error,
                )
            )
        out.append(Message(role=msg.role, content=new_content) if changed else msg)
    return out


__all__ = [
    "classify_content",
    "exceeds_tool_output_budget",
    "shape_json",
    "shape_logs",
    "shape_messages_tool_results",
    "shape_tool_text",
]
