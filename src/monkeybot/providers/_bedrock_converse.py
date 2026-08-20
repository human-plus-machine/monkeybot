"""Bedrock Converse helpers for non-Claude models (Grok, Nova, Llama, …)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

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
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    estimate_anthropic_input_tokens,
    iter_stream_with_param_retry,
    safe_parse_tool_args,
    split_leading_system,
)
from monkeybot.providers.model_capabilities import supports_param

_log = logging.getLogger(__name__)

_GEO_PREFIXES = frozenset({"us", "eu", "apac", "ap", "global", "au", "jp", "ca", "us-gov"})
_STRIPPABLE_INFERENCE = ("temperature",)
_UNSUPPORTED_FIELD_RE = re.compile(r"doesn't support the (\w+) field", re.IGNORECASE)
_SENTINEL = object()
_DOC_FORMATS = {
    "pdf": "pdf",
    "csv": "csv",
    "html": "html",
    "md": "md",
    "markdown": "md",
    "plain": "txt",
    "txt": "txt",
    "doc": "doc",
    "docx": "docx",
    "xls": "xls",
    "xlsx": "xlsx",
}


def _strip_bedrock_prefix(model: str) -> str:
    name = model.strip()
    if name.lower().startswith("bedrock/"):
        return name.split("/", 1)[1]
    return name


def bedrock_vendor(model: str) -> str:
    """Vendor token from a Bedrock model / inference-profile id.

    ``us.xai.grok-4.6`` → ``xai``, ``us.anthropic.claude-…`` → ``anthropic``,
    ``vendor/model`` and ``vendor.model`` forms, ``bedrock/`` prefix, and bare
    ``claude-…`` (Anthropic).
    """
    name = _strip_bedrock_prefix(model)
    if not name:
        return ""
    lower = name.lower()
    if lower.startswith("claude-") or lower.startswith("claude."):
        return "anthropic"
    if "/" in name:
        return name.split("/", 1)[0].lower()
    parts = name.split(".")
    if len(parts) >= 2 and parts[0].lower() in _GEO_PREFIXES:
        return parts[1].lower()
    if len(parts) >= 2:
        return parts[0].lower()
    return parts[0].lower()


def uses_anthropic_bedrock(model: str) -> bool:
    """True when this id should use ``AsyncAnthropicBedrock``, not Converse."""
    return bedrock_vendor(model) == "anthropic"


def converse_tools(tools: Sequence[ToolDef]) -> dict[str, Any] | None:
    """Converse ``toolConfig``: ``tools[].toolSpec`` with ``inputSchema.json``."""
    if not tools:
        return None
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": {"json": t.input_schema},
                }
            }
            for t in tools
        ]
    }


def _image_format(mime_type: str) -> str:
    subtype = mime_type.rsplit("/", 1)[-1].lower()
    if subtype in {"jpg", "jpeg"}:
        return "jpeg"
    if subtype in {"png", "gif", "webp"}:
        return subtype
    return "png"


def _document_format(mime_type: str) -> str:
    return _DOC_FORMATS.get(mime_type.rsplit("/", 1)[-1].lower(), "txt")


def _document_name(block: File) -> str:
    meta = block.metadata or {}
    raw = meta.get("filename") or meta.get("name") or "document"
    if not isinstance(raw, str) or not raw.strip():
        return "document"
    return raw.strip()[:200]


def _image_block(block: Image) -> dict[str, Any]:
    return {
        "image": {
            "format": _image_format(block.mime_type),
            "source": {"bytes": base64.b64decode(block.data)},
        }
    }


def _document_block(block: File) -> dict[str, Any]:
    return {
        "document": {
            "format": _document_format(block.mime_type),
            "name": _document_name(block),
            "source": {"bytes": base64.b64decode(block.data)},
        }
    }


def _tool_result_content(result: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in result:
        if isinstance(block, Text):
            out.append({"text": block.text})
        elif isinstance(block, Image):
            out.append(_image_block(block))
        elif isinstance(block, File):
            out.append(_document_block(block))
        else:
            raise ValueError(f"unsupported ToolResponse block for Converse: {type(block).__name__}")
    return out or [{"text": ""}]


def _converse_block(block: Any) -> dict[str, Any] | None:
    if isinstance(block, Thinking):
        inner: dict[str, Any] = {"text": block.thinking}
        if block.signature:
            inner["signature"] = block.signature
        return {"reasoningContent": {"reasoningText": inner}}
    if isinstance(block, RedactedThinking):
        return {"reasoningContent": {"redactedContent": block.data}}
    if isinstance(block, Text):
        return {"text": block.text}
    if isinstance(block, Image):
        return _image_block(block)
    if isinstance(block, File):
        return _document_block(block)
    if isinstance(block, ToolRequest):
        return {
            "toolUse": {
                "toolUseId": block.id,
                "name": block.name,
                "input": dict(block.args),
            }
        }
    if isinstance(block, ToolResponse):
        result: dict[str, Any] = {
            "toolUseId": block.id,
            "content": _tool_result_content(block.result),
        }
        if block.is_error:
            result["status"] = "error"
        return {"toolResult": result}
    raise ValueError(f"unsupported content block for Converse: {type(block).__name__}")


def messages_to_converse(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split leading system; map harness blocks; merge consecutive same-role turns."""
    system, rest = split_leading_system(messages)
    out: list[dict[str, Any]] = []
    for m in rest:
        if m.role not in ("user", "assistant"):
            raise ValueError(f"unsupported role for Converse messages: {m.role!r}")
        content: list[dict[str, Any]] = []
        for block in m.content:
            mapped = _converse_block(block)
            if mapped is not None:
                content.append(mapped)
        if not content:
            continue
        if out and out[-1]["role"] == m.role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": m.role, "content": content})
    if not out or out[0]["role"] != "user":
        # Converse requires the first message to be user (Anthropic does not).
        out.insert(0, {"role": "user", "content": [{"text": "(continued)"}]})
    return system, out


def converse_request_kwargs(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Build ``converse_stream`` kwargs. Always sets ``inferenceConfig.maxTokens``.

    ``hints.cache_retention`` / Converse ``cachePoint`` blocks are not wired on
    this path. Claude still uses ``prepare_anthropic_cached_payload``; Nova/Llama
    cache points are a follow-up. ``UsageEvent.cache_read_tokens`` staying 0
    means caching was never requested, not that the model refused a cache hit.
    """
    system, converse_msgs = messages_to_converse(messages)
    inference: dict[str, Any] = {"maxTokens": max_tokens}
    if supports_param(model, "temperature"):
        inference["temperature"] = temperature
    kwargs: dict[str, Any] = {
        "modelId": _strip_bedrock_prefix(model),
        "messages": converse_msgs,
        "inferenceConfig": inference,
    }
    if system:
        kwargs["system"] = [{"text": system}]
    tool_config = converse_tools(tools)
    if tool_config is not None:
        kwargs["toolConfig"] = tool_config
    return kwargs


def _copy_stream_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    out = dict(kwargs)
    infer = out.get("inferenceConfig")
    if isinstance(infer, dict):
        out["inferenceConfig"] = dict(infer)
    return out


def _strip_unsupported_inference(kwargs: dict[str, Any], exc: Exception) -> list[str]:
    match = _UNSUPPORTED_FIELD_RE.search(str(exc))
    if not match or match.group(1).lower() != "temperature":
        return []
    infer = kwargs.get("inferenceConfig")
    if not isinstance(infer, dict) or "temperature" not in infer:
        return []
    infer = dict(infer)
    del infer["temperature"]
    kwargs["inferenceConfig"] = infer
    return ["temperature"]


def _tool_input_fragment(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


def _reasoning_delta(delta: dict[str, Any]) -> ThinkingDelta | None:
    rc = delta.get("reasoningContent")
    if not isinstance(rc, dict):
        return None
    text = rc.get("text") or ""
    signature = rc.get("signature")
    inner = rc.get("reasoningText")
    if isinstance(inner, dict):
        text = text or inner.get("text") or ""
        signature = signature or inner.get("signature")
    if not text and not signature:
        return None
    return ThinkingDelta(text=str(text) if text else "", signature=signature)


class _ToolBuf:
    __slots__ = ("call_id", "name", "buf")

    def __init__(self, call_id: str, name: str) -> None:
        self.call_id = call_id
        self.name = name
        self.buf = ""


async def _aiter_sync(sync_iterable: Any) -> AsyncIterator[Any]:
    it = iter(sync_iterable)
    while True:
        item = await asyncio.to_thread(next, it, _SENTINEL)
        if item is _SENTINEL:
            break
        yield item


async def _close_event_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        await asyncio.to_thread(close)


def _on_content_block_delta(
    delta_ev: Any,
    tools: dict[int, _ToolBuf],
) -> Iterator[ProviderEvent]:
    if not isinstance(delta_ev, dict):
        _log.warning(
            "unexpected converse stream event %s",
            kv(kind="contentBlockDelta", type=type(delta_ev).__name__),
        )
        return
    idx = int(delta_ev.get("contentBlockIndex") or 0)
    delta = delta_ev.get("delta") or {}
    if not isinstance(delta, dict):
        _log.warning(
            "unexpected converse stream event %s",
            kv(kind="contentBlockDelta.delta", type=type(delta).__name__),
        )
        return
    text = delta.get("text")
    if text:
        yield TextDelta(text=text)
    thinking = _reasoning_delta(delta)
    if thinking is not None:
        yield thinking
    tool_delta = delta.get("toolUse")
    if isinstance(tool_delta, dict):
        buf = tools.get(idx)
        fragment = _tool_input_fragment(tool_delta.get("input"))
        if buf is not None and fragment:
            buf.buf += fragment
            yield ToolInputDelta(call_id=buf.call_id, name=buf.name, delta=fragment)


def _on_content_block_stop(
    stop_ev: Any,
    tools: dict[int, _ToolBuf],
    *,
    provider: str,
) -> Iterator[ProviderEvent]:
    idx = int(stop_ev.get("contentBlockIndex") or 0) if isinstance(stop_ev, dict) else 0
    buf = tools.pop(idx, None)
    if buf is None:
        return
    if not buf.call_id:
        _log.warning(
            "converse toolUse missing toolUseId %s",
            kv(provider=provider, name=buf.name),
        )
    args, parse_error = safe_parse_tool_args(
        buf.buf,
        call_id=buf.call_id,
        tool_name=buf.name,
        provider=provider,
    )
    yield ToolCall(call_id=buf.call_id, name=buf.name, args=args, parse_error=parse_error)


def _on_content_block_start(
    start_ev: Any,
    tools: dict[int, _ToolBuf],
    *,
    provider: str,
) -> None:
    if not isinstance(start_ev, dict):
        return
    start = start_ev.get("start") or {}
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    if not isinstance(tool_use, dict):
        return
    idx = int(start_ev.get("contentBlockIndex") or 0)
    call_id = str(tool_use.get("toolUseId") or "")
    name = str(tool_use.get("name") or "")
    if not call_id:
        _log.warning(
            "converse toolUse missing toolUseId %s",
            kv(provider=provider, name=name),
        )
    tools[idx] = _ToolBuf(call_id, name)


async def _iter_converse_events(
    stream: Any,
    *,
    provider: str,
) -> AsyncIterator[ProviderEvent]:
    tools: dict[int, _ToolBuf] = {}
    input_tokens = output_tokens = cache_read = cache_creation = 0
    stop_reason: str | None = None
    async for event in _aiter_sync(stream):
        if not isinstance(event, dict):
            _log.warning(
                "unexpected converse stream event %s",
                kv(kind="event", type=type(event).__name__),
            )
            continue
        if "contentBlockStart" in event:
            _on_content_block_start(event["contentBlockStart"], tools, provider=provider)
        elif "contentBlockDelta" in event:
            for item in _on_content_block_delta(event["contentBlockDelta"], tools):
                yield item
        elif "contentBlockStop" in event:
            for item in _on_content_block_stop(event["contentBlockStop"], tools, provider=provider):
                yield item
        elif "messageStop" in event:
            sr = (event["messageStop"] or {}).get("stopReason")
            if sr:
                stop_reason = str(sr)
        elif "metadata" in event:
            usage = (event["metadata"] or {}).get("usage") or {}
            input_tokens = int(usage.get("inputTokens") or 0)
            output_tokens = int(usage.get("outputTokens") or 0)
            cache_read = int(usage.get("cacheReadInputTokens") or 0)
            cache_creation = int(usage.get("cacheWriteInputTokens") or 0)
    yield UsageEvent(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cached_tokens=cache_read + cache_creation,
    )
    yield Done(truncated=stop_reason == "max_tokens")


async def _stream_converse_once(
    client: Any,
    kwargs: dict[str, Any],
    *,
    provider: str,
) -> AsyncIterator[ProviderEvent]:
    response = await asyncio.to_thread(client.converse_stream, **kwargs)
    stream = response["stream"]
    try:
        async for event in _iter_converse_events(stream, provider=provider):
            yield event
    finally:
        await _close_event_stream(stream)


async def iter_converse_stream(
    client: Any,
    stream_kwargs: dict[str, Any],
    *,
    provider: str,
    error_message: str,
    n_messages: int | None = None,
    n_tools: int | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Stream ``converse_stream``, retrying once a rejected inference field is stripped."""
    kwargs = _copy_stream_kwargs(stream_kwargs)
    async for event in iter_stream_with_param_retry(
        lambda kw: _stream_converse_once(client, kw, provider=provider),
        kwargs,
        strip_rejected=_strip_unsupported_inference,
        max_attempts=len(_STRIPPABLE_INFERENCE) + 1,
        provider=provider,
        error_message=error_message,
        model=kwargs.get("modelId"),
        n_messages=n_messages,
        n_tools=n_tools,
        retry_message="converse stream rejected params, retrying without them %s",
    ):
        yield event


def _estimate_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, Text):
        return {"text": block.text}
    if isinstance(block, Thinking):
        return {"thinking": block.thinking}
    if isinstance(block, Image):
        return {"image": block.mime_type, "chars": len(block.data)}
    if isinstance(block, File):
        return {"file": block.mime_type, "chars": len(block.data)}
    if isinstance(block, ToolRequest):
        return {"toolRequest": {"id": block.id, "name": block.name, "args": dict(block.args)}}
    if isinstance(block, ToolResponse):
        return {
            "toolResponse": {
                "id": block.id,
                "result": [_estimate_block(b) for b in block.result],
            }
        }
    return {"type": type(block).__name__}


def estimate_converse_input_tokens(
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
) -> int:
    """Character estimate from harness messages. CountTokens is Anthropic-only."""
    system, rest = split_leading_system(messages)
    serializable = [
        {"role": m.role, "content": [_estimate_block(b) for b in m.content]} for m in rest
    ]
    tool_defs = [t.to_model_schema() for t in tools] if tools else None
    return estimate_anthropic_input_tokens(
        system=system,
        messages=serializable,
        tools=tool_defs,
    )
