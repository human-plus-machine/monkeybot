"""Shared provider utilities (cost estimation, Anthropic message shaping)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Literal

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolInputDelta,
    UsageEvent,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.prompts.headings import VOLATILE_SECTION_MARKERS
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    File,
    Image,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
from monkeybot.providers.pricing import estimate_cost

_log = logging.getLogger(__name__)


def safe_parse_tool_args(
    raw: str,
    *,
    call_id: str,
    tool_name: str,
    provider: str,
) -> tuple[dict[str, object], str | None]:
    """Parse streamed tool arguments; return ``({}, error)`` when JSON is invalid."""
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        action = "malformed_tool_args"
        error = f"malformed tool args JSON: {exc}"
    else:
        if isinstance(parsed, dict):
            return parsed, None
        action = "non_object_tool_args"
        error = f"tool args must be a JSON object, got {type(parsed).__name__}"
    _log.warning(
        "stream_parse_repair %s",
        kv(
            action=action,
            call_id=call_id,
            tool_name=tool_name,
            provider=provider,
        ),
    )
    return {}, error


def anthropic_tool_defs(tools: Sequence[Any]) -> list[dict[str, Any]]:
    return [t.to_model_schema() for t in tools]


def split_leading_system(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    msgs = list(messages)
    if not msgs or msgs[0].role != "system":
        return "", msgs
    system = "\n\n".join(b.text for b in msgs[0].content if isinstance(b, Text))
    return system, msgs[1:]


async def count_anthropic_input_tokens(
    client: Any,
    *,
    anthropic_module: Any,
    model: str,
    system: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
) -> int:
    resp = await client.messages.count_tokens(
        model=model,
        system=system or anthropic_module.NOT_GIVEN,
        messages=messages,
        tools=tools if tools else anthropic_module.NOT_GIVEN,
    )
    return int(resp.input_tokens)


# Sampling/thinking kwargs a request may carry that a not-yet-catalogued model
# can reject with HTTP 400. Model-specific cases are gated ahead of time via
# ``model_capabilities.supports_param``; this is the fallback for a model that
# table doesn't know about yet (e.g. released after this code was written).
_STRIPPABLE_PARAMS = ("temperature", "thinking")


def _rejects_param(msg: str, param: str) -> bool:
    """True if ``msg`` blames ``param`` as a request field, not just mentions the word.

    Anthropic reports a rejected param as a field path — ``temperature: Extra
    inputs are not permitted``, ``thinking.budget_tokens: ...``. A bare
    substring test would also match unrelated 400s that merely name the
    concept, e.g. the message-shaping error "When 'thinking' is enabled, a
    final 'assistant' message must start with a thinking block" — stripping
    there would silently disable extended thinking and hide a real bug.
    """
    return re.search(rf"(?:^|[^a-z0-9_.]){re.escape(param)}(?:\.[a-z0-9_.]+)?:", msg) is not None


def _strip_rejected_params(kwargs: dict[str, Any], exc: Exception) -> list[str]:
    """Drop any of ``_STRIPPABLE_PARAMS`` blamed by ``exc``; return what was dropped."""
    msg = str(exc).lower()
    if "400" not in msg:
        return []
    dropped = [p for p in _STRIPPABLE_PARAMS if p in kwargs and _rejects_param(msg, p)]
    for p in dropped:
        del kwargs[p]
    return dropped


async def iter_stream_with_param_retry(
    stream_once: Callable[[dict[str, Any]], AsyncIterator[ProviderEvent]],
    kwargs: dict[str, Any],
    *,
    strip_rejected: Callable[[dict[str, Any], Exception], list[str]],
    max_attempts: int,
    provider: str,
    error_message: str,
    model: object,
    n_messages: int | None = None,
    n_tools: int | None = None,
    retry_message: str,
) -> AsyncIterator[ProviderEvent]:
    """Run ``stream_once``; if it fails before any event, strip a blamed param and retry."""
    for _attempt in range(max_attempts):
        started = False
        try:
            async for event in stream_once(kwargs):
                started = True
                yield event
            return
        except Exception as exc:
            if not started:
                dropped = strip_rejected(kwargs, exc)
                if dropped:
                    _log.warning(
                        retry_message,
                        kv(provider=provider, model=model, dropped=dropped),
                    )
                    continue
            _log.warning(
                error_message,
                kv(
                    provider=provider,
                    model=model,
                    n_messages=n_messages,
                    n_tools=n_tools,
                ),
                exc_info=True,
            )
            raise


async def iter_anthropic_sdk_stream(
    client: Any,
    stream_kwargs: dict[str, Any],
    *,
    provider: str,
    error_message: str,
    n_messages: int | None = None,
    n_tools: int | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Stream an Anthropic ``messages.stream`` call, yielding provider events.

    If the request 400s before any content was streamed and the error blames a
    sampling/thinking param present in ``stream_kwargs``, retries with that
    param dropped — a safety net for models not yet in ``model_capabilities``
    (see that module's docstring). Retries until no further param can be
    dropped, since the API blames one field at a time and a request can carry
    several rejected params at once. No retry once content has started:
    dropping a param mid-stream would risk duplicated output for a caller that
    already received deltas.
    """
    kwargs = dict(stream_kwargs)  # never mutate the caller's dict
    async for event in iter_stream_with_param_retry(
        lambda kw: _stream_anthropic_once(client, kw, provider=provider),
        kwargs,
        strip_rejected=_strip_rejected_params,
        max_attempts=len(_STRIPPABLE_PARAMS) + 1,
        provider=provider,
        error_message=error_message,
        model=kwargs.get("model"),
        n_messages=n_messages,
        n_tools=n_tools,
        retry_message="anthropic stream rejected params, retrying without them %s",
    ):
        yield event


async def _stream_anthropic_once(
    client: Any,
    stream_kwargs: dict[str, Any],
    *,
    provider: str,
) -> AsyncIterator[ProviderEvent]:
    tool_input_buf = ""
    tool_id = ""
    tool_name = ""
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0
    stop_reason: str | None = None

    async with client.messages.stream(**stream_kwargs) as stream:
        async for event in stream:
            match event.type:
                case "content_block_start":
                    if event.content_block.type == "tool_use":
                        tool_id = event.content_block.id
                        tool_name = event.content_block.name
                        tool_input_buf = ""
                case "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield TextDelta(text=event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        thought = getattr(event.delta, "thinking", None) or ""
                        if thought:
                            yield ThinkingDelta(text=thought)
                    elif event.delta.type == "signature_delta":
                        sig = getattr(event.delta, "signature", None) or ""
                        if sig:
                            yield ThinkingDelta(text="", signature=sig)
                    elif event.delta.type == "input_json_delta":
                        partial = event.delta.partial_json
                        tool_input_buf += partial
                        if tool_id and partial:
                            yield ToolInputDelta(
                                call_id=tool_id,
                                name=tool_name,
                                delta=partial,
                            )
                case "content_block_stop":
                    if tool_id:
                        args, parse_error = safe_parse_tool_args(
                            tool_input_buf,
                            call_id=tool_id,
                            tool_name=tool_name,
                            provider=provider,
                        )
                        yield ToolCall(
                            call_id=tool_id,
                            name=tool_name,
                            args=args,
                            parse_error=parse_error,
                        )
                        tool_id = tool_name = tool_input_buf = ""
                case "message_delta":
                    delta = getattr(event, "delta", None)
                    sr = getattr(delta, "stop_reason", None) if delta is not None else None
                    if sr:
                        stop_reason = str(sr)
                    if hasattr(event, "usage"):
                        output_tokens = int(getattr(event.usage, "output_tokens", 0) or 0)
                        read = int(getattr(event.usage, "cache_read_input_tokens", 0) or 0)
                        created = int(getattr(event.usage, "cache_creation_input_tokens", 0) or 0)
                        if read:
                            cache_read = read
                        if created:
                            cache_creation = created
                case "message_start":
                    if hasattr(event, "message") and event.message.usage:
                        usage = event.message.usage
                        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    yield UsageEvent(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cached_tokens=cache_read + cache_creation,
    )
    yield Done(truncated=stop_reason == "max_tokens")


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
        elif isinstance(block, File):
            out.append(
                {
                    "type": "document",
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
    if isinstance(block, File):
        return {
            "type": "document",
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
    raise ValueError(f"unsupported assistant content block for Anthropic: {type(block).__name__}")


# Volatile section headings emitted by ``compose_system_prompt``
# (``core.prompts.prompt``), ``_append_extra_system_text`` (``core.runtime.loop``),
# and ``_format_system_context_update`` (``core.context.epoch``). Shared from the
# import-free ``core.prompts.headings`` leaf: importing the composing modules
# here would cycle (``core.context`` -> ``core.config.settings`` -> provider
# classes in this package), which is why these literals were once duplicated by
# hand — and drifted. Markers are title-line only, so editing the prose under a
# heading cannot move the split point.
_VOLATILE_SYSTEM_MARKERS = VOLATILE_SECTION_MARKERS


def split_system_prompt_for_cache(system: str) -> tuple[str, str]:
    """Split composed system text into stable (cacheable) prefix and volatile tail."""
    split_at = len(system)
    for marker in _VOLATILE_SYSTEM_MARKERS:
        idx = system.find(marker)
        if idx != -1:
            split_at = min(split_at, idx)
    if split_at >= len(system):
        return system, ""
    return system[:split_at], system[split_at:]


def anthropic_cache_control(
    cache_retention: Literal["none", "short", "long"],
) -> dict[str, Any] | None:
    """Build an Anthropic ``cache_control`` object, or ``None`` when caching is disabled.

    ``short`` uses the default 5-minute ephemeral TTL. ``long`` sets ``ttl: "1h"``
    (extended cache; no beta header required on current Anthropic APIs).
    """
    if cache_retention == "none":
        return None
    if cache_retention == "long":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def build_cached_system_blocks(
    system: str,
    *,
    cache_retention: Literal["none", "short", "long"] = "short",
) -> list[dict[str, Any]]:
    """Return Anthropic system blocks with cache_control only on the stable prefix.

    Volatile tail sections (current date, memory, skills, current request) are sent in a second
    uncached block so explicit caching hits across curation turns.

    When ``cache_retention`` is ``none``, no ``cache_control`` markers are applied.

    Args:
        system: Non-empty system prompt text. Callers MUST guard empty strings
            and pass anthropic.NOT_GIVEN instead (see provider stream methods).
        cache_retention: ``none`` disables markers; ``short`` is 5-minute ephemeral;
            ``long`` is 1-hour ephemeral (``ttl: "1h"``).

    Returns:
        One or two text blocks; the stable prefix carries ``cache_control`` unless
        retention is ``none``.
    """
    stable, volatile = split_system_prompt_for_cache(system)
    control = anthropic_cache_control(cache_retention)
    if control is None:
        if not volatile.strip():
            return [{"type": "text", "text": system}]
        blocks: list[dict[str, Any]] = [{"type": "text", "text": stable}]
        if volatile:
            blocks.append({"type": "text", "text": volatile})
        return blocks
    if not volatile.strip():
        return [{"type": "text", "text": system, "cache_control": control}]
    blocks = [
        {"type": "text", "text": stable, "cache_control": control},
    ]
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


def mark_last_tool_cached(
    tools: list[dict[str, Any]],
    *,
    cache_retention: Literal["none", "short", "long"] = "short",
) -> list[dict[str, Any]]:
    """Return a copy of ``tools`` with ``cache_control`` on the LAST tool.

    Needed when system is empty (no system breakpoint). No-ops when ``tools`` is
    empty or ``cache_retention`` is ``none``.

    Args:
        tools: Anthropic tool dicts (output of a provider ``_convert_tools``).
        cache_retention: When ``none``, returns tools unchanged (no markers).

    Returns:
        A new list; only the last element gains a ``cache_control`` key. Input
        list and its dicts are not mutated (shallow-copy the last dict).
    """
    control = anthropic_cache_control(cache_retention)
    if not tools or control is None:
        return tools
    marked_last = {**tools[-1], "cache_control": control}
    return [*tools[:-1], marked_last]


def _mark_message_last_block_cached(
    message: dict[str, Any],
    *,
    control: dict[str, Any],
) -> dict[str, Any]:
    """Return a shallow-copied message with ``cache_control`` on its last content block."""
    content = message.get("content")
    if isinstance(content, str):
        return {
            **message,
            "content": [{"type": "text", "text": content, "cache_control": control}],
        }
    if not isinstance(content, list) or not content:
        return dict(message)
    blocks = [dict(b) if isinstance(b, dict) else b for b in content]
    last = blocks[-1]
    if isinstance(last, dict):
        blocks[-1] = {**last, "cache_control": control}
    return {**message, "content": blocks}


def mark_conversation_cache_breakpoints(
    messages: list[dict[str, Any]],
    *,
    cache_retention: Literal["none", "short", "long"] = "short",
    max_breakpoints: int = 2,
) -> list[dict[str, Any]]:
    """Return messages with rolling ``cache_control`` breakpoints on the conversation prefix.

    Marks the last content block of the most recent message and retains a breakpoint
    on the previous message (when present) so the turn that *writes* a new cache
    entry can still *read* the prior prefix. Anthropic allows at most 4 breakpoints
    per request; callers should budget system/tools markers accordingly.

    Does not mutate caller-owned dicts. When ``cache_retention`` is ``none`` or
    ``max_breakpoints`` < 1, returns a shallow copy of ``messages`` unchanged.
    """
    if not messages or cache_retention == "none" or max_breakpoints < 1:
        return list(messages)
    control = anthropic_cache_control(cache_retention)
    if control is None:
        return list(messages)

    out = [dict(m) for m in messages]
    # Mark from the end: newest first, then previous turn's end.
    n = min(max_breakpoints, len(out))
    for idx in range(len(out) - n, len(out)):
        out[idx] = _mark_message_last_block_cached(out[idx], control=control)
    return out


def prepare_anthropic_cached_payload(
    *,
    system: str,
    messages: Sequence[Message],
    tools: Sequence[Any],
    cache_retention: Literal["none", "short", "long"],
    not_given: Any,
    conversation_breakpoints: int = 2,
) -> tuple[Any, list[dict[str, Any]], Any]:
    """Build ``(system_param, messages, tools_param)`` for Anthropic ``messages.stream``.

    Shared by Claude / Bedrock / Vertex Claude so marker placement stays identical.
    Budget: 1 system + 1 tools + ``conversation_breakpoints`` (default 2) ≤ 4.
    """
    converted_messages = mark_conversation_cache_breakpoints(
        build_anthropic_messages(messages),
        cache_retention=cache_retention,
        max_breakpoints=conversation_breakpoints,
    )
    converted_tools = anthropic_tool_defs(tools) if tools else None
    system_param: Any = (
        build_cached_system_blocks(system, cache_retention=cache_retention) if system else not_given
    )
    tools_param: Any = (
        mark_last_tool_cached(converted_tools, cache_retention=cache_retention)
        if converted_tools
        else not_given
    )
    return system_param, converted_messages, tools_param


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


# Default chars/token for Bedrock/Vertex estimate fallback. ``// 4`` under-counted
# JSON-heavy tool payloads enough that compaction fired ~60 turns late in a
# measured 164-turn Bedrock session; 3 is a conservative baseline before feedback.
_ESTIMATE_CHARS_PER_TOKEN = 3.0
# Multiplier learned from preflight estimate vs provider-reported prompt size.
_estimate_correction: float = 1.0


def note_anthropic_token_estimate_observation(*, estimated: int, actual: int) -> None:
    """Feed back last-turn estimate vs actual so future estimates track real size.

    ``actual`` should be total prompt tokens (uncached input + cache read + cache
    creation). Uses a light EMA so a single outlier cannot swing the scale.
    """
    global _estimate_correction
    if estimated < 1 or actual < 1:
        return
    ratio = actual / estimated
    # Clamp so a broken count cannot zero or explode the scale.
    ratio = max(0.5, min(ratio, 4.0))
    _estimate_correction = (0.7 * _estimate_correction) + (0.3 * ratio)


def reset_anthropic_token_estimate_correction() -> None:
    """Test helper: restore the estimate correction factor to 1.0."""
    global _estimate_correction
    _estimate_correction = 1.0


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
    base = max(1, int(char_count / _ESTIMATE_CHARS_PER_TOKEN))
    return max(1, int(base * _estimate_correction))


__all__ = [
    "anthropic_cache_control",
    "anthropic_tool_defs",
    "build_anthropic_messages",
    "build_cached_system_blocks",
    "count_anthropic_input_tokens",
    "estimate_anthropic_input_tokens",
    "estimate_cost",
    "iter_anthropic_sdk_stream",
    "iter_stream_with_param_retry",
    "mark_conversation_cache_breakpoints",
    "mark_last_tool_cached",
    "note_anthropic_token_estimate_observation",
    "prepare_anthropic_cached_payload",
    "reset_anthropic_token_estimate_correction",
    "safe_parse_tool_args",
    "split_leading_system",
    "split_system_prompt_for_cache",
]
