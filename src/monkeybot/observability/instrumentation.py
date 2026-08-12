"""Extended instrumentation: provider wrapper, FastAPI, hook span events, trace id helpers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import (
    Message,
    Provider,
    ProviderEvent,
    ToolDef,
    provider_count_input_tokens,
    provider_stream,
)
from monkeybot.observability._state import is_observability_enabled
from monkeybot.observability.spans import span_llm

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI

HookPhase = Literal["pre_tool", "post_tool"]


class ObservingProvider:
    """Wraps a :class:`~monkeybot.core.llm.provider.Provider` for direct (non-loop) ``stream`` calls."""

    def __init__(self, inner: Provider, *, turn_ctx: TurnContext | None = None) -> None:
        self._inner = inner
        self._turn_ctx = turn_ctx

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def supports_streaming(self) -> bool:
        return self._inner.supports_streaming

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> int:
        return await provider_count_input_tokens(
            self._inner,
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        )

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        inner_stream = provider_stream(
            self._inner,
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        )
        if not is_observability_enabled():
            return inner_stream

        async def _observed() -> AsyncIterator[ProviderEvent]:
            ctx = self._correlation_ctx(model)
            async with span_llm(
                ctx=ctx,
                model=model,
                vertex_google_search=vertex_google_search,
            ):
                async for evt in inner_stream:
                    yield evt

        return _observed()

    def _correlation_ctx(self, model: str) -> TurnContext:
        if self._turn_ctx is not None:
            return self._turn_ctx
        return TurnContext(
            thread_id="direct",
            request_id="direct",
            agent_md="",
            memory_index=[],
            skills=[],
            tools=[],
            user_id=None,
            parent_run_id=None,
            model=model,
        )


def instrument_fastapi_app(app: object) -> None:
    """Instrument a FastAPI app with OTel HTTP server spans (idempotent, defensive)."""
    if not is_observability_enabled():
        return
    if getattr(app, "_monkeybot_otel_fastapi_instrumented", False):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning(
            "FastAPI OpenTelemetry instrumentation unavailable (install [observability] extra)"
        )
        return
    FastAPIInstrumentor().instrument_app(cast("FastAPI", app))
    cast(Any, app)._monkeybot_otel_fastapi_instrumented = True


def get_current_trace_id_hex_optional() -> str | None:
    """Return W3C trace id (32 lower-hex chars) for the current span, or ``None``."""
    if not is_observability_enabled():
        return None
    from opentelemetry import trace
    from opentelemetry.trace import format_trace_id

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return format_trace_id(ctx.trace_id)


def add_tool_hook_span_event(*, phase: HookPhase, tool_name: str) -> None:
    """Record a hook lifecycle event on the active span (``monkeybot.tool`` when in loop)."""
    if not is_observability_enabled():
        return
    from opentelemetry import trace

    event_name = "monkeybot.hook.pre_tool" if phase == "pre_tool" else "monkeybot.hook.post_tool"
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.add_event(event_name, {"tool.name": tool_name})
