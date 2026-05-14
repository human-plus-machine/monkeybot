"""
Provider Protocol — the model integration contract.
One streaming method. Zero extra ceremony.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .context import TurnContext


@dataclass
class Message:
    """A single message in the conversation history.

    Attributes:
        role: Speaker role — "user", "assistant", or "tool".
        content: Message content.
        tool_call_id: For role="tool", the originating call ID.
        tool_name: For role="tool", the tool that produced this message.
    """

    role: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class ToolDef:
    """Definition of a tool available to the model.

    Attributes:
        name: Unique tool name.
        description: Human-readable description.
        parameters: JSON Schema describing the tool's input.
    """

    name: str
    description: str
    parameters: dict  # type: ignore[type-arg]


@dataclass
class TextDelta:
    """A partial text chunk streamed from the model.

    Attributes:
        text: Partial content string.
    """

    text: str


@dataclass
class ToolCall:
    """A tool invocation requested by the model.

    Attributes:
        call_id: Unique identifier for this call.
        name: Name of the tool to invoke.
        args: Arguments to pass to the tool.
    """

    call_id: str
    name: str
    args: dict  # type: ignore[type-arg]


@dataclass
class ProviderUsage:
    """Token and cost accounting for a completed provider request.

    Attributes:
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        cached_tokens: Number of tokens served from cache.
        cost_usd: Estimated cost in US dollars.
    """

    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ProviderDone:
    """Signals that the provider stream is complete.

    Attributes:
        usage: Token and cost accounting.
    """

    usage: ProviderUsage


ProviderEvent = TextDelta | ToolCall | ProviderDone


@runtime_checkable
class Provider(Protocol):
    """Protocol that all model provider adapters must implement.

    Use @runtime_checkable so isinstance() checks work without subclassing.
    """

    @property
    def name(self) -> str:
        """Unique provider identifier (e.g. "claude", "gemini")."""
        ...

    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming responses."""
        ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str,
        system: str,
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream model responses as ProviderEvent objects.

        Args:
            messages: Conversation history.
            tools: Tool definitions available to the model.
            model: Model identifier string.
            system: System prompt text.
            context: Optional turn context with memory and skills.

        Returns:
            An async iterator of ProviderEvent objects.
        """
        ...
