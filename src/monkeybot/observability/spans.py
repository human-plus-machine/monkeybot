"""Span helpers for the agent loop (lazy OpenTelemetry imports)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.observability._state import is_observability_enabled

_TRUNC_SUFFIX = "…[truncated]"
_DEFAULT_MAX_BYTES = 8192

_DENYLIST_SUBSTRINGS = (
    "api_key",
    "secret",
    "password",
    "token",
    "authorization",
    "credential",
    "private_key",
)

_ATTR_ALLOWLIST_EXACT = frozenset(
    {
        "tool.name",
        "gen_ai.request.model",
        "gen_ai.operation.name",
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cached_tokens",
        "gen_ai.usage.cache_read_tokens",
        "gen_ai.usage.cache_creation_tokens",
        "gen_ai.usage.total_tokens",
        "turn.index",
        "turns.summarized",
        "trace.input",
        "trace.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
        "langfuse.observation.input",
        "langfuse.observation.output",
        "input.value",
        "output.value",
        "openinference.span.kind",
    }
)

# OpenInference span kinds (Phoenix UI): https://arize.com/docs/phoenix/tracing
_OI_KIND_AGENT = "AGENT"
_OI_KIND_CHAIN = "CHAIN"
_OI_KIND_LLM = "LLM"
_OI_KIND_TOOL = "TOOL"

_tool_outcome: dict[str, tuple[str | None, str | None] | None] = {"value": None}


@dataclass
class _SpanHandle:
    span: Any | None
    token: Any | None


def truncate(value: str, *, max_bytes: int = _DEFAULT_MAX_BYTES) -> str:
    if not value:
        return value
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + _TRUNC_SUFFIX


def _key_denied(key: str) -> bool:
    lower = key.lower()
    if lower.startswith("gen_ai."):
        return False
    return any(part in lower for part in _DENYLIST_SUBSTRINGS)


def set_span_attribute_safe(
    span: Any,
    key: str,
    value: str | int | bool | float,
) -> None:
    if _key_denied(key):
        return
    if isinstance(value, str) and key not in _ATTR_ALLOWLIST_EXACT:
        value = truncate(value)
    span.set_attribute(key, value)


def _current_span() -> Any | None:
    if not is_observability_enabled():
        return None
    from opentelemetry import trace

    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return None
    return span


def _set_openinference_span_kind(span: Any, kind: str) -> None:
    set_span_attribute_safe(span, "openinference.span.kind", kind)


def _mirror_observation_io(
    span: Any,
    *,
    input_value: str | None = None,
    output_value: str | None = None,
    trace_level: bool = False,
) -> None:
    """Mirror I/O for Langfuse (langfuse.*) and Phoenix (input.value / output.value)."""
    if input_value is not None:
        msg = truncate(input_value)
        set_span_attribute_safe(span, "langfuse.observation.input", msg)
        set_span_attribute_safe(span, "input.value", msg)
        if trace_level:
            set_span_attribute_safe(span, "langfuse.trace.input", msg)
    if output_value is not None:
        msg = truncate(output_value)
        set_span_attribute_safe(span, "langfuse.observation.output", msg)
        set_span_attribute_safe(span, "output.value", msg)
        if trace_level:
            set_span_attribute_safe(span, "langfuse.trace.output", msg)


def set_run_output(text: str) -> None:
    span = _current_span()
    if span is None:
        return
    out = truncate(text)
    set_span_attribute_safe(span, "trace.output", out)
    _mirror_observation_io(span, output_value=text, trace_level=True)


def set_llm_io(*, prompt: str, completion: str) -> None:
    """Set LLM span I/O for Langfuse generations (call inside ``span_llm``)."""
    span = _current_span()
    if span is None:
        return
    prompt_t = truncate(prompt)
    completion_t = truncate(completion)
    set_span_attribute_safe(span, "gen_ai.prompt", prompt_t)
    set_span_attribute_safe(span, "gen_ai.completion", completion_t)
    _mirror_observation_io(span, input_value=prompt, output_value=completion)


def set_turn_io(
    *,
    input_value: str | None = None,
    output_value: str | None = None,
) -> None:
    """Set turn span I/O (call on the active ``monkeybot.turn`` span before ``end_turn_span``)."""
    span = _current_span()
    if span is None:
        return
    if input_value is not None:
        set_span_attribute_safe(span, "trace.input", truncate(input_value))
    _mirror_observation_io(span, input_value=input_value, output_value=output_value)


def set_turn_prompt_tokens(count: int) -> None:
    span = _current_span()
    if span is None:
        return
    set_span_attribute_safe(span, "estimated_prompt_tokens", count)


def set_llm_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> None:
    span = _current_span()
    if span is None:
        return
    set_span_attribute_safe(span, "gen_ai.usage.input_tokens", input_tokens)
    set_span_attribute_safe(span, "gen_ai.usage.output_tokens", output_tokens)
    if cached_tokens:
        set_span_attribute_safe(span, "gen_ai.usage.cached_tokens", cached_tokens)
    if cache_read_tokens:
        set_span_attribute_safe(span, "gen_ai.usage.cache_read_tokens", cache_read_tokens)
    if cache_creation_tokens:
        set_span_attribute_safe(
            span, "gen_ai.usage.cache_creation_tokens", cache_creation_tokens
        )
    total = input_tokens + output_tokens
    if total:
        set_span_attribute_safe(span, "gen_ai.usage.total_tokens", total)


def set_summarize_turns(count: int) -> None:
    span = _current_span()
    if span is None:
        return
    set_span_attribute_safe(span, "turns.summarized", count)


def _record_tool_outcome(result_text: str | None, error_text: str | None) -> None:
    _tool_outcome["value"] = (result_text, error_text)


def _agent_name() -> str:
    return os.environ.get("AGENT_NAME", "monkeybot")


@asynccontextmanager
async def span_run(
    ctx: TurnContext,
    *,
    user_message: str,
    agent_name: str | None = None,
) -> AsyncGenerator[None, None]:
    if not is_observability_enabled():
        yield
        return

    from monkeybot.observability import get_tracer

    tracer = get_tracer()
    msg = truncate(user_message)
    with tracer.start_as_current_span("monkeybot.run") as span:
        _set_openinference_span_kind(span, _OI_KIND_AGENT)
        set_span_attribute_safe(span, "thread.id", ctx.thread_id)
        set_span_attribute_safe(span, "request.id", ctx.request_id)
        set_span_attribute_safe(span, "user.message", msg)
        set_span_attribute_safe(span, "trace.input", msg)
        _mirror_observation_io(span, input_value=user_message, trace_level=True)
        set_span_attribute_safe(span, "agent.name", agent_name or _agent_name())
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
            raise


def begin_turn_span(
    *,
    turn_index: int,
    thread_id: str,
    request_id: str,
) -> _SpanHandle:
    if not is_observability_enabled():
        return _SpanHandle(None, None)

    from opentelemetry import trace as trace_api
    from opentelemetry.context import attach

    from monkeybot.observability import get_tracer

    tracer = get_tracer()
    span = tracer.start_span("monkeybot.turn")
    _set_openinference_span_kind(span, _OI_KIND_CHAIN)
    set_span_attribute_safe(span, "turn.index", turn_index)
    set_span_attribute_safe(span, "thread.id", thread_id)
    set_span_attribute_safe(span, "request.id", request_id)
    otel_ctx = trace_api.set_span_in_context(span)
    token = attach(otel_ctx)
    return _SpanHandle(span, token)


def end_turn_span(handle: _SpanHandle) -> None:
    if handle.span is None:
        return
    from opentelemetry.context import detach

    if handle.token is not None:
        detach(handle.token)
    handle.span.end()


@asynccontextmanager
async def span_turn(
    *,
    turn_index: int,
    thread_id: str,
    request_id: str,
) -> AsyncGenerator[None, None]:
    handle = begin_turn_span(
        turn_index=turn_index,
        thread_id=thread_id,
        request_id=request_id,
    )
    try:
        yield
    except Exception as exc:
        if handle.span is not None:
            handle.span.record_exception(exc)
            trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
            handle.span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
        raise
    finally:
        end_turn_span(handle)


@asynccontextmanager
async def span_llm(
    *,
    ctx: TurnContext,
    model: str | None = None,
) -> AsyncGenerator[None, None]:
    if not is_observability_enabled():
        yield
        return

    from monkeybot.observability import get_tracer

    tracer = get_tracer()
    model_id = model or ctx.model
    with tracer.start_as_current_span("monkeybot.llm.stream") as span:
        _set_openinference_span_kind(span, _OI_KIND_LLM)
        set_span_attribute_safe(span, "gen_ai.request.model", model_id)
        set_span_attribute_safe(span, "gen_ai.operation.name", "chat")
        set_span_attribute_safe(span, "thread.id", ctx.thread_id)
        set_span_attribute_safe(span, "request.id", ctx.request_id)
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
            raise


@asynccontextmanager
async def span_tool(
    *,
    tool_name: str,
    tool_call_id: str,
    thread_id: str,
    request_id: str,
    args: dict[str, object] | None = None,
) -> AsyncGenerator[None, None]:
    if not is_observability_enabled():
        yield
        return

    from monkeybot.observability import get_tracer

    _tool_outcome["value"] = None
    tracer = get_tracer()
    with tracer.start_as_current_span("monkeybot.tool") as span:
        _set_openinference_span_kind(span, _OI_KIND_TOOL)
        set_span_attribute_safe(span, "tool.name", tool_name)
        set_span_attribute_safe(span, "tool.call_id", tool_call_id)
        set_span_attribute_safe(span, "thread.id", thread_id)
        set_span_attribute_safe(span, "request.id", request_id)
        if args is not None:
            args_json = truncate(json.dumps(args, default=str))
            set_span_attribute_safe(span, "tool.input", args_json)
            _mirror_observation_io(span, input_value=args_json)
        try:
            yield
            outcome = _tool_outcome["value"]
            if outcome is not None:
                result_text, error_text = outcome
                if error_text:
                    set_span_attribute_safe(span, "error", error_text)
                    trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
                    span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, error_text))
                elif result_text is not None:
                    out = truncate(result_text)
                    set_span_attribute_safe(span, "tool.output", out)
                    _mirror_observation_io(span, output_value=out)
        except Exception as exc:
            set_span_attribute_safe(span, "error", truncate(str(exc)))
            span.record_exception(exc)
            trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
            raise
        finally:
            _tool_outcome["value"] = None


def record_tool_outcome(result_text: str | None, error_text: str | None) -> None:
    """Called from the loop after ``tool_executor.execute`` inside ``span_tool``."""
    _record_tool_outcome(result_text, error_text)


@asynccontextmanager
async def span_subagent(
    *,
    thread_id: str,
    request_id: str,
    parent_run_id: str,
    subagent_type: str | None = None,
    agent_md: str | None = None,
) -> AsyncGenerator[None, None]:
    """Child span for subprocess ``task`` run; nests under propagated context when attached."""
    if not is_observability_enabled():
        yield
        return

    from monkeybot.observability import get_tracer

    tracer = get_tracer()
    with tracer.start_as_current_span("monkeybot.subagent") as span:
        _set_openinference_span_kind(span, _OI_KIND_AGENT)
        set_span_attribute_safe(span, "thread.id", thread_id)
        set_span_attribute_safe(span, "request.id", request_id)
        set_span_attribute_safe(span, "parent.run.id", parent_run_id)
        if subagent_type:
            set_span_attribute_safe(span, "subagent.type", subagent_type)
        if agent_md:
            set_span_attribute_safe(span, "subagent.agent_md", agent_md)
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
            raise


@asynccontextmanager
async def span_summarize(
    *,
    thread_id: str,
    request_id: str,
) -> AsyncGenerator[None, None]:
    if not is_observability_enabled():
        yield
        return

    from monkeybot.observability import get_tracer

    tracer = get_tracer()
    with tracer.start_as_current_span("monkeybot.context.summarize") as span:
        _set_openinference_span_kind(span, _OI_KIND_CHAIN)
        set_span_attribute_safe(span, "thread.id", thread_id)
        set_span_attribute_safe(span, "request.id", request_id)
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            trace_api = __import__("opentelemetry.trace", fromlist=["Status", "StatusCode"])
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
            raise
