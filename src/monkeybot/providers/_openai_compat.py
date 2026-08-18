"""Shared helpers for OpenAI-compatible Chat Completions providers.

Used by OpenAIProvider, HuggingFaceProvider, and OllamaProvider — same wire
format, different auth/URLs. ``count_input_tokens_tiktoken`` and
``stream_chat_completions_with_tool_fallback`` provide full method bodies for
providers whose ``count_input_tokens``/``stream`` differ only by base URL,
API key, and provider name (HuggingFace, Ollama).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import random
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
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
from monkeybot.providers._utils import safe_parse_tool_args

_log = logging.getLogger(__name__)


def _delta_reasoning_text(delta: Any) -> str:
    """Extract thinking/reasoning text from an OpenAI-compat stream delta.

    Ollama and some other servers put reasoning on ``delta.reasoning`` (or
    ``thinking``). Pydantic models may park unknown fields in ``model_extra``.
    """
    for key in ("reasoning", "thinking", "reasoning_content"):
        value = getattr(delta, key, None)
        if isinstance(value, str) and value:
            return value
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning", "thinking", "reasoning_content"):
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _system_prompt_from_message(message: Message) -> str:
    texts = [b.text for b in message.content if isinstance(b, Text)]
    return "\n\n".join(texts)


def _file_label(block: Image | File) -> str:
    """Shared path/filename lookup for the placeholder/extracted-text builders below."""
    meta = block.metadata or {}
    label = meta.get("path") or meta.get("filename")
    return str(label) if label else ""


def _media_tool_placeholder(kind: str, block: Image | File) -> str:
    """Text stand-in for image/file tool results on text-only OpenAI-compat models.

    Chat Completions tool messages are text-only; pixels are already delivered to
    the UI via ``ImageBlock`` SSE. Keep a short path/filename hint for the model.
    """
    label = _file_label(block)
    label_bit = f", path={label}" if label else ""
    return (
        f"[{kind} loaded: mime={block.mime_type}{label_bit}, "
        f"~{len(block.data or '')} base64 chars — pixels omitted for this provider; "
        "already shown in the UI. Describe this media from the user request or "
        "generation prompt used this turn; do not invent a different subject from "
        "memory or prior sessions.]"
    )


def _user_file_placeholder(block: File) -> str:
    """Text stand-in for a user-attached file OpenAI-compat can't ingest.

    Chat Completions has no document wire type. Unlike ``_media_tool_placeholder``
    (tool-result media already streamed to the UI as pixels), the model has never
    seen these bytes — say so plainly instead of inviting it to "describe" content
    it was never given, which just prompts a hallucinated summary. Used when
    extraction isn't applicable (non-PDF) or found no text (scanned/image-only PDF).
    """
    label = _file_label(block)
    label_bit = f" ({label})" if label else ""
    return (
        f"[File attachment{label_bit}, mime={block.mime_type}: this provider cannot "
        "read file contents over the chat API. Tell the user this attachment type "
        "isn't supported here instead of guessing at its contents.]"
    )


_MAX_EXTRACTED_PDF_CHARS = 20_000
_PDF_TEXT_CACHE_MAX = 32
# Hash → extracted text (or None). Avoids lru_cache retaining full base64 keys.
_PDF_TEXT_CACHE: OrderedDict[tuple[str, int], str | None] = OrderedDict()


def _extract_pdf_text_sync(data_b64: str, max_chars: int) -> str | None:
    """Best-effort text layer extraction. Returns None on any failure or no text.

    Bytes are an untrusted user upload (may be corrupt, encrypted, or a scanned
    image-only PDF with no text layer) — any of that is a normal "nothing to
    extract" outcome, not a bug, so failures fall back to the caller's placeholder
    rather than raising. Cache is keyed by content sha256 so repeated history walks
    do not re-parse the same attachment or retain giant base64 cache keys.
    """
    from pypdf import PdfReader  # noqa: PLC0415

    digest = hashlib.sha256(data_b64.encode("ascii", errors="ignore")).hexdigest()
    cache_key = (digest, max_chars)
    if cache_key in _PDF_TEXT_CACHE:
        _PDF_TEXT_CACHE.move_to_end(cache_key)
        return _PDF_TEXT_CACHE[cache_key]

    try:
        reader = PdfReader(io.BytesIO(base64.b64decode(data_b64)))
    except Exception:
        _log.warning("PDF extraction failed to open document", exc_info=True)
        _PDF_TEXT_CACHE[cache_key] = None
        _PDF_TEXT_CACHE.move_to_end(cache_key)
        while len(_PDF_TEXT_CACHE) > _PDF_TEXT_CACHE_MAX:
            _PDF_TEXT_CACHE.popitem(last=False)
        return None

    parts: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            _log.warning("PDF extraction failed on one page, skipping it", exc_info=True)
            continue
        if text:
            parts.append(text)
            total += len(text)
        if total >= max_chars:
            break
    text = "\n\n".join(parts).strip()
    result = text or None
    _PDF_TEXT_CACHE[cache_key] = result
    _PDF_TEXT_CACHE.move_to_end(cache_key)
    while len(_PDF_TEXT_CACHE) > _PDF_TEXT_CACHE_MAX:
        _PDF_TEXT_CACHE.popitem(last=False)
    return result


async def _extract_pdf_text(block: File, max_chars: int = _MAX_EXTRACTED_PDF_CHARS) -> str | None:
    """Off-thread PDF text extraction so parsing never blocks the gateway's event loop."""
    if block.mime_type != "application/pdf":
        return None
    text = await asyncio.to_thread(_extract_pdf_text_sync, block.data, max_chars)
    if text and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[... PDF text truncated ...]"
    return text


def _extracted_file_text(block: File, text: str) -> str:
    label = _file_label(block)
    label_bit = f" ({label})" if label else ""
    return f"[File attachment{label_bit}, extracted text follows:]\n{text}"


async def _user_file_content(block: File) -> str:
    """Real extracted text when available, else the existing can't-read placeholder."""
    extracted = await _extract_pdf_text(block)
    if extracted:
        return _extracted_file_text(block, extracted)
    return _user_file_placeholder(block)


async def _flatten_tool_response_text(block: ToolResponse) -> str:
    parts: list[str] = []
    for b in block.result:
        if isinstance(b, Text):
            parts.append(b.text)
        elif isinstance(b, Image):
            parts.append(_media_tool_placeholder("image", b))
        elif isinstance(b, File):
            extracted = await _extract_pdf_text(b)
            parts.append(_extracted_file_text(b, extracted) if extracted else _media_tool_placeholder("file", b))
        else:
            raise ValueError(
                f"unsupported ToolResponse block for OpenAI-compat: {type(b).__name__}"
            )
    return "".join(parts)


async def messages_to_openai(
    messages: Sequence[Message],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split system prompt text vs OpenAI Chat messages (block-native)."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    async def flush_user_blocks(buf: list[ContentBlock]) -> None:
        if not buf:
            return
        if len(buf) == 1 and isinstance(buf[0], Text):
            out.append({"role": "user", "content": buf[0].text})
            return
        content: list[dict[str, Any]] = []
        for item in buf:
            if isinstance(item, Text):
                content.append({"type": "text", "text": item.text})
            elif isinstance(item, Image):
                # Text-only OpenAI-compat models/servers may reject image_url
                # outright; that surfaces as a normal upstream error, not a
                # crash here. No capability check — model vision support isn't
                # known ahead of the request.
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{item.mime_type};base64,{item.data}"},
                    }
                )
            elif isinstance(item, File):
                content.append({"type": "text", "text": await _user_file_content(item)})
            else:
                raise ValueError(
                    f"unsupported user content block for OpenAI-compat: {type(item).__name__}"
                )
        out.append({"role": "user", "content": content})

    for m in messages:
        if m.role == "system":
            sys_txt = _system_prompt_from_message(m)
            if sys_txt.strip():
                system_parts.append(sys_txt)
            continue

        if m.role == "assistant":
            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in m.content:
                if isinstance(block, Text):
                    text_chunks.append(block.text)
                elif isinstance(block, ToolRequest):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(dict(block.args), ensure_ascii=False),
                            },
                        }
                    )
                elif isinstance(block, (Thinking, RedactedThinking)):
                    # Chat Completions has no thinking wire type; drop so Ollama /
                    # HF / OpenAI can continue after a turn that stored reasoning.
                    continue
                else:
                    raise ValueError(
                        f"unsupported assistant block for OpenAI-compat: {type(block).__name__}"
                    )
            row: dict[str, Any] = {"role": "assistant"}
            if len(text_chunks) == 1:
                row["content"] = text_chunks[0]
            elif len(text_chunks) > 1:
                row["content"] = "\n\n".join(text_chunks)
            elif not tool_calls:
                row["content"] = None
            else:
                row["content"] = None
            if tool_calls:
                row["tool_calls"] = tool_calls
            out.append(row)
            continue

        if m.role != "user":
            raise ValueError(f"unsupported role for OpenAI-compat: {m.role!r}")

        buf: list[ContentBlock] = []
        for block in m.content:
            if isinstance(block, ToolResponse):
                await flush_user_blocks(buf)
                buf.clear()
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.id,
                        "content": await _flatten_tool_response_text(block),
                    }
                )
            else:
                buf.append(block)
        await flush_user_blocks(buf)

    joined_system = "\n\n".join(system_parts).strip()
    return (joined_system or None, out)


def openai_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        schema = t.to_model_schema()
        out.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["input_schema"],
                },
            }
        )
    return out


def openai_messages_token_count(encoding: Any, oai_messages: list[dict[str, Any]]) -> int:
    """tiktoken-based estimate for Chat Completions-shaped message dicts."""
    total = 0
    for m in oai_messages:
        total += 4
        for key, val in m.items():
            if val is None:
                continue
            if key == "tool_calls" and isinstance(val, list):
                for tc in val:
                    total += len(
                        encoding.encode(json.dumps(tc, ensure_ascii=False, default=str))
                    )
            elif isinstance(val, str):
                total += len(encoding.encode(val))
            else:
                total += len(encoding.encode(json.dumps(val, ensure_ascii=False, default=str)))
    total += 2
    return total


def openai_tools_token_count(encoding: Any, tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    return len(encoding.encode(json.dumps(tools, ensure_ascii=False, default=str)))


async def count_openai_compat_input_tokens(
    encoding: Any,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
) -> int:
    system, oai_messages = await messages_to_openai(messages)
    if system:
        oai_messages = [{"role": "system", "content": system}, *oai_messages]
    tool_defs = openai_tools(tools) if tools else []
    return openai_messages_token_count(encoding, oai_messages) + openai_tools_token_count(
        encoding, tool_defs
    )


_STREAM_USAGE_OPTIONS: dict[str, bool] = {"include_usage": True}

_RATE_LIMIT_MAX_ATTEMPTS = 3
_RATE_LIMIT_BASE_DELAY_S = 1.0


def _rate_limit_retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter: ~1s, ~2s, ~4s, … for attempts 1, 2, 3."""
    base: float = _RATE_LIMIT_BASE_DELAY_S * (2 ** (attempt - 1))
    jitter: float = random.uniform(0, 0.5)
    return base + jitter

# NVIDIA's free build.nvidia.com tier is low-throughput and returns its own
# worker/quota text instead of a clean 429, so callers that fan out concurrent
# requests (parallel tool calls, subagents) can self-inflict a rate limit. Cap
# in-flight requests for providers with a known low tier; others are unbounded.
_LOW_THROUGHPUT_PROVIDER_CONCURRENCY: dict[str, int] = {"nvidia": 4}
# Keyed by (provider, event loop): a Semaphore is bound to the loop it waits on,
# so reusing one across loops (repeated asyncio.run, per-test event loops) raises.
_provider_semaphores: dict[tuple[str, asyncio.AbstractEventLoop], asyncio.Semaphore] = {}


class ProviderRateLimitError(Exception):
    """Raised when an upstream provider's own rate limit is hit and retries are exhausted.

    ``str(self)`` is a user-facing message; the raw upstream error is chained via
    ``__cause__`` (see ``raise ... from exc``) for logs, not shown to the user.
    """

    def __init__(self, provider: str, model: str, original: BaseException) -> None:
        super().__init__(
            f"{provider} is temporarily rate-limiting requests for model {model!r}. "
            "Please wait a moment and try again."
        )
        self.provider = provider
        self.model = model
        self.original = original


_SERVER_ERROR_MAX_ATTEMPTS = 3


class ProviderServerError(Exception):
    """Raised when an upstream provider returns a persistent 5xx and retries are exhausted.

    ``str(self)`` is a user-facing message; the raw upstream error is chained via
    ``__cause__`` (see ``raise ... from exc``) for logs, not shown to the user.
    """

    def __init__(self, provider: str, model: str, original: BaseException) -> None:
        super().__init__(
            f"{provider} returned a server error for model {model!r}. "
            "Please wait a moment and try again."
        )
        self.provider = provider
        self.model = model
        self.original = original


def is_server_error(exc: BaseException) -> bool:
    """Return True when *exc* is a transient upstream 5xx (not a client/config error).

    Some OpenAI-compat backends (e.g. NVIDIA's build.nvidia.com NIM endpoints)
    return a bare 500 with a generic body like "Internal server error" instead of
    a structured error. Left unhandled, that bare SDK message becomes the *only*
    diagnostic surfaced to the caller (see ``task`` tool results with
    ``tool_call_count: 0`` and ``errors: ["Internal server error"]``). Treat it as
    transient and retry with backoff, same as a rate limit.

    Two distinct shapes observed in practice, both must be covered:
    1. A clean HTTP 5xx before streaming starts — ``openai.APIStatusError`` with
       a concrete ``status_code``.
    2. A *mid-stream* protocol-level failure — the initial HTTP response is a
       200, but an error chunk arrives inside the SSE body, which the OpenAI
       SDK surfaces as a bare ``openai.APIError`` (the base class) with no
       ``status_code`` attribute at all. Confirmed live against NVIDIA's
       nemotron endpoint: ``openai.APIError: Internal server error`` raised
       from ``openai/_streaming.py``'s ``__stream__``, not ``APIStatusError``.
    """
    try:
        from openai import APIError, APIStatusError  # noqa: PLC0415

        if isinstance(exc, APIStatusError):
            return exc.status_code is not None and bool(exc.status_code >= 500)
        if isinstance(exc, APIError):
            # No status_code on this shape — the failure is generic-server-ish
            # text NVIDIA emits mid-stream, not a validation/client error.
            msg = str(getattr(exc, "message", None) or exc).lower()
            return "internal server error" in msg
    except ImportError:
        pass
    return False


def is_rate_limit_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like an upstream rate-limit / capacity error.

    Covers standard OpenAI-SDK 429s (``RateLimitError``) plus backends like NVIDIA
    that return their own quota text (e.g. "ResourceExhausted: Worker local total
    request limit reached (N/M)") without a clean 429 status.
    """
    try:
        from openai import APIStatusError, RateLimitError  # noqa: PLC0415

        if isinstance(exc, RateLimitError):
            return True
        if isinstance(exc, APIStatusError) and exc.status_code == 429:
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in (
            # NVIDIA's build.nvidia.com worker/quota text, e.g. "ResourceExhausted:
            # Worker local total request limit reached (N/M)". Deliberately specific
            # (not a bare "rate limit" substring) to avoid misclassifying unrelated
            # errors that happen to mention rate limits in passing.
            "resourceexhausted",
            "worker local total request limit",
            "too many requests",
        )
    )


def _provider_semaphore(provider: str) -> asyncio.Semaphore | None:
    # ponytail: entries never evicted when a loop closes (fine for a gateway
    # process, which has one loop for its lifetime); add eviction if a caller
    # starts creating many short-lived loops in one process.
    limit = _LOW_THROUGHPUT_PROVIDER_CONCURRENCY.get(provider)
    if limit is None:
        return None
    key = (provider, asyncio.get_running_loop())
    if key not in _provider_semaphores:
        _provider_semaphores[key] = asyncio.Semaphore(limit)
    return _provider_semaphores[key]


_TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _extract_text_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Best-effort recovery for tool calls emitted as literal ``<tool_call>`` text.

    Some OpenAI-compat backends (e.g. NIM/vLLM-hosted OSS models like NVIDIA's
    nemotron line) emit a call as inline text using the Hermes-style
    ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>`` convention
    instead of populating the structured ``delta.tool_calls`` field, when the
    server's chat template isn't wired for native tool-calling. Left
    unhandled, that text is indistinguishable from a normal reply — it's
    shown to the user verbatim and the tool never actually runs (observed in
    the wild: ``assistant_text`` containing literal ``<tool_call>...`` XML,
    ``tool_call_count: 0``). Only called when no structured tool call arrived,
    so this never overrides a well-behaved provider's real response.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for match in _TEXT_TOOL_CALL_RE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = payload.get("arguments", {})
        if not isinstance(args, dict):
            continue
        found.append((name, args))
    return found


async def iter_openai_compat_stream(
    client: Any,
    kwargs: dict[str, Any],
    *,
    provider: str = "openai_compat",
    n_messages: int | None = None,
    n_tools: int | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Yield ProviderEvents from an OpenAI-compatible streaming chat request.

    Handles text deltas, tool call buffering, usage accounting, and Done.
    ``client`` must be an ``AsyncOpenAI``-compatible object.
    """
    req = dict(kwargs)
    if req.get("stream") and "stream_options" not in req:
        req["stream_options"] = dict(_STREAM_USAGE_OPTIONS)

    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    tool_buf: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    text_chunks: list[str] = []

    try:
        stream = await client.chat.completions.create(**req)
        async for chunk in stream:
            if chunk.usage is not None:
                input_tokens = int(chunk.usage.prompt_tokens or 0)
                output_tokens = int(chunk.usage.completion_tokens or 0)
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = str(fr)
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                text_chunks.append(delta.content)
                yield TextDelta(text=delta.content)
            reasoning = _delta_reasoning_text(delta)
            if reasoning:
                yield ThinkingDelta(text=reasoning)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = int(tc.index or 0)
                    slot = tool_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["args"] += tc.function.arguments

        for slot in tool_buf.values():
            name = str(slot.get("name") or "")
            if not name:
                continue
            tid = str(slot.get("id") or "") or f"anon:{name}"
            raw_args = str(slot.get("args") or "{}")
            parsed, parse_error = safe_parse_tool_args(
                raw_args,
                call_id=tid,
                tool_name=name,
                provider="openai_compat",
            )
            yield ToolCall(call_id=tid, name=name, args=parsed, parse_error=parse_error)

        if not tool_buf and n_tools:
            # No structured tool call arrived even though tools were offered —
            # check for a Hermes-style literal <tool_call> block before
            # treating this as a plain text reply. See _extract_text_tool_calls.
            recovered = _extract_text_tool_calls("".join(text_chunks))
            for name, args in recovered:
                _log.warning(
                    "recovered literal <tool_call> text as a real tool call %s",
                    kv(provider=provider, model=kwargs.get("model"), tool=name),
                )
                yield ToolCall(call_id=f"anon:{name}", name=name, args=args, parse_error=None)
    except Exception:
        _log.warning(
            "OpenAI-compat stream error %s",
            kv(
                provider=provider,
                model=kwargs.get("model"),
                n_messages=n_messages,
                n_tools=n_tools,
            ),
            exc_info=True,
        )
        raise

    # OpenAI reports prompt_tokens incl. cached; subtract to avoid double-count (see 1C).
    yield UsageEvent(
        input_tokens=max(0, input_tokens - cached_tokens),
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_read_tokens=cached_tokens,
        cache_creation_tokens=0,
    )
    yield Done(truncated=finish_reason == "length")


async def count_input_tokens_tiktoken(
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    model: str,
    thinking_budget: int | None = None,
) -> int:
    """Shared ``count_input_tokens`` body for tiktoken-based providers.

    Falls back to ``cl100k_base`` when ``model`` isn't a tiktoken-known model id
    (true for most non-OpenAI models served through an OpenAI-compat endpoint).
    ``thinking_budget`` is accepted for ``Provider`` protocol symmetry but unused:
    none of these providers' token counts vary with reasoning configuration.
    """
    del thinking_budget
    import tiktoken  # noqa: PLC0415

    msgs = list(messages)
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    return await count_openai_compat_input_tokens(enc, msgs, tools)


async def stream_chat_completions_with_tool_fallback(
    *,
    base_url: str,
    api_key: str,
    provider: str,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    model: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Shared ``stream`` body for OpenAI-compat providers.

    Retries without tools when the upstream server rejects function calling
    (HuggingFace, Ollama, …). Also retries with backoff on rate-limit/capacity
    errors (raw upstream body may not be a clean 429, e.g. NVIDIA's own
    "ResourceExhausted" text) and caps in-flight concurrency per provider; see
    ``is_rate_limit_error`` / ``_provider_semaphore``.
    """
    from openai import AsyncOpenAI  # noqa: PLC0415

    msgs = list(messages)
    system, oai_messages = await messages_to_openai(msgs)
    if system:
        oai_messages = [{"role": "system", "content": system}, *oai_messages]

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = openai_tools(tools)
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    n_tools = len(tools)
    sem = _provider_semaphore(provider)
    # Each counts its own failure kind only, independent of the other retry
    # paths (e.g. switching to a tools-less request shouldn't burn a
    # rate-limit retry, and a rate limit shouldn't burn a server-error retry).
    rate_limit_attempts = 0
    server_error_attempts = 0
    while True:
        yielded_any = False
        retry_delay: float | None = None
        async with sem if sem is not None else contextlib.nullcontext():
            try:
                async for event in iter_openai_compat_stream(
                    client,
                    kwargs,
                    provider=provider,
                    n_messages=len(messages),
                    n_tools=n_tools,
                ):
                    yielded_any = True
                    yield event
                return
            except Exception as exc:
                if n_tools and is_tool_unsupported_error(exc):
                    _log.warning(
                        "%s model %r does not support tool calling; "
                        "retrying without tools. Error: %s",
                        provider,
                        model,
                        exc,
                    )
                    kwargs.pop("tools", None)
                    n_tools = 0
                    continue
                if is_rate_limit_error(exc):
                    if yielded_any or rate_limit_attempts >= _RATE_LIMIT_MAX_ATTEMPTS - 1:
                        # Already streamed partial output this attempt, or retries
                        # exhausted: can't safely retry, but still hide the raw
                        # upstream text (e.g. NVIDIA's internal worker/quota string).
                        raise ProviderRateLimitError(provider, model, exc) from exc
                    rate_limit_attempts += 1
                    retry_delay = _rate_limit_retry_delay(rate_limit_attempts)
                    _log.warning(
                        "%s rate-limited (attempt %d/%d); retrying in %.1fs: %s",
                        provider,
                        rate_limit_attempts,
                        _RATE_LIMIT_MAX_ATTEMPTS,
                        retry_delay,
                        exc,
                    )
                elif is_server_error(exc):
                    if yielded_any or server_error_attempts >= _SERVER_ERROR_MAX_ATTEMPTS - 1:
                        # Already streamed partial output this attempt, or retries
                        # exhausted: surface a clear message instead of a bare
                        # upstream 500 body (e.g. NVIDIA's "Internal server error").
                        raise ProviderServerError(provider, model, exc) from exc
                    server_error_attempts += 1
                    retry_delay = _rate_limit_retry_delay(server_error_attempts)
                    _log.warning(
                        "%s server error (attempt %d/%d); retrying in %.1fs: %s",
                        provider,
                        server_error_attempts,
                        _SERVER_ERROR_MAX_ATTEMPTS,
                        retry_delay,
                        exc,
                    )
                else:
                    raise
        # Sleep outside the concurrency gate: a backing-off request must not
        # hold an in-flight slot idle, or a burst of rate-limited callers can
        # fill every slot with sleepers and starve everyone else.
        if retry_delay is not None:
            await asyncio.sleep(retry_delay)


def is_tool_unsupported_error(exc: BaseException) -> bool:
    """Return True when the exception likely indicates unsupported tool calling.

    Shared by providers (HuggingFace, Ollama, …) that fall back to a tool-less
    request when the upstream model/server rejects function calling.
    """
    try:
        from openai import APIStatusError  # noqa: PLC0415
    except ImportError:
        msg = str(exc).lower()
        return "tool" in msg and "unsupported" in msg

    if isinstance(exc, APIStatusError):
        if exc.status_code not in (400, 422):
            return False
        body = str(exc.body or exc.message or "").lower()
        return any(
            phrase in body
            for phrase in (
                "tool",
                "function_call",
                "functions",
                "unsupported",
            )
        )

    msg = str(exc).lower()
    return "tool" in msg and "unsupported" in msg


__all__ = [
    "ProviderRateLimitError",
    "ProviderServerError",
    "count_input_tokens_tiktoken",
    "count_openai_compat_input_tokens",
    "is_rate_limit_error",
    "is_server_error",
    "is_tool_unsupported_error",
    "iter_openai_compat_stream",
    "messages_to_openai",
    "openai_messages_token_count",
    "openai_tools",
    "openai_tools_token_count",
    "stream_chat_completions_with_tool_fallback",
]
