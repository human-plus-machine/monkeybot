"""Map provider stream events to agent events with text/thinking block bounds."""

from __future__ import annotations

from monkeybot.core.llm.provider import (
    Done,
    GroundingEvent as ProviderGroundingEvent,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolInputDelta,
    UsageEvent,
)
from monkeybot.core.runtime.events import (
    AgentEvent,
    AssistantDelta,
    AssistantTextEnded,
    AssistantTextStarted,
    GroundingEvent,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ThinkingBlockStarted,
    ToolInputDeltaEvent,
)


class ProviderStreamMapper:
    """Stateful mapper: provider deltas → additive agent streaming events.

    Accumulates assistant/thinking text and pending tool calls. Callers handle
    ``UsageEvent`` cost accounting themselves.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.assistant_text = ""
        self.thinking_text = ""
        self.thinking_signature: str | None = None
        self.thinking_streamed = False
        self.pending: dict[str, ToolCall] = {}
        self.stream_truncated = False
        self._thinking_closed = False
        self._thinking_started = False
        self._text_started = False
        self._text_ended = False

    def map(self, ev: ProviderEvent) -> list[AgentEvent]:
        """Map one provider event. ``Done`` sets ``stream_truncated`` and returns ``[]``."""
        if isinstance(ev, TextDelta):
            return self._on_text_delta(ev)
        if isinstance(ev, ThinkingDelta):
            return self._on_thinking_delta(ev)
        if isinstance(ev, ToolInputDelta):
            return [
                ToolInputDeltaEvent(
                    request_id=self.request_id,
                    call_id=ev.call_id,
                    tool=ev.name,
                    delta=ev.delta,
                )
            ]
        if isinstance(ev, ToolCall):
            out = self._close_open_blocks()
            self.pending[ev.call_id] = ev
            return out
        if isinstance(ev, ProviderGroundingEvent):
            return [
                GroundingEvent(
                    request_id=self.request_id,
                    sources=[dict(s) for s in ev.sources],
                    search_queries=list(ev.search_queries),
                )
            ]
        if isinstance(ev, Done):
            self.stream_truncated = ev.truncated
            return []
        if isinstance(ev, UsageEvent):
            return []
        return []

    def finish(self) -> list[AgentEvent]:
        """Close any open text/thinking blocks at end of stream."""
        return self._close_open_blocks()

    def _close_open_blocks(self) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        ended = self._ensure_text_ended()
        if ended is not None:
            out.append(ended)
        closed = self._close_thinking_block()
        if closed is not None:
            out.append(closed)
        return out

    def _on_text_delta(self, ev: TextDelta) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        closed = self._close_thinking_block()
        if closed is not None:
            out.append(closed)
        started = self._ensure_text_started()
        if started is not None:
            out.append(started)
        self.assistant_text += ev.text
        if ev.text:
            out.append(AssistantDelta(request_id=self.request_id, delta=ev.text))
        return out

    def _on_thinking_delta(self, ev: ThinkingDelta) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        started = self._ensure_thinking_started()
        if started is not None:
            out.append(started)
        self.thinking_text += ev.text
        if ev.signature:
            self.thinking_signature = ev.signature
        if ev.text:
            self.thinking_streamed = True
            out.append(
                ThinkingBlockDelta(
                    request_id=self.request_id,
                    text=ev.text,
                    signature=ev.signature,
                )
            )
        return out

    def _ensure_text_started(self) -> AssistantTextStarted | None:
        if self._text_started:
            return None
        self._text_started = True
        return AssistantTextStarted(request_id=self.request_id)

    def _ensure_text_ended(self) -> AssistantTextEnded | None:
        if not self._text_started or self._text_ended:
            return None
        self._text_ended = True
        return AssistantTextEnded(request_id=self.request_id)

    def _ensure_thinking_started(self) -> ThinkingBlockStarted | None:
        if self._thinking_started:
            return None
        self._thinking_started = True
        return ThinkingBlockStarted(request_id=self.request_id)

    def _close_thinking_block(self) -> ThinkingBlockComplete | None:
        if not self.thinking_streamed or self._thinking_closed:
            return None
        self._thinking_closed = True
        return ThinkingBlockComplete(
            request_id=self.request_id,
            signature=self.thinking_signature or "",
        )
