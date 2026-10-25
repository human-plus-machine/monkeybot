"""Scripted providers shared by loop tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

from monkeybot.core.llm.provider import Message, ProviderEvent
from monkeybot.core.testing.mocks_provider import fake_provider_prompt_tokens
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef


class FakeProvider:
    def __init__(self, scripted: list[list[object]], *, name: str = "fake") -> None:
        self._scripted = scripted
        self._name = name
        self.stream_calls = 0
        self.stream_models: list[str] = []
        self.stream_messages: list[list[Message]] = []
        self.count_thinking_budgets: list[int | None] = []
        self.vertex_google_search_calls: list[bool] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def supports_streaming(self) -> bool:
        return True

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        del tools, thinking_budget
        self.stream_messages.append(list(messages))
        self.stream_models.append(model)
        self.vertex_google_search_calls.append(vertex_google_search)
        idx = self.stream_calls
        self.stream_calls += 1
        if idx >= len(self._scripted):
            return
        for ev in self._scripted[idx]:
            yield cast(ProviderEvent, ev)

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> int:
        del model, vertex_google_search
        self.count_thinking_budgets.append(thinking_budget)
        return int(fake_provider_prompt_tokens(messages, tools))


class CapturingProvider:
    """Records the system message text passed in for each ``stream()`` call."""

    def __init__(self, scripted: list[list[ProviderEvent]]) -> None:
        self._scripted = scripted
        self.system_texts: list[str] = []
        self.tool_names: list[list[str]] = []
        self.message_snapshots: list[list[Message]] = []
        self._idx = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supports_streaming(self) -> bool:
        return True

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del model, thinking_budget
        system = next((m for m in messages if m.role == "system"), None)
        text = ""
        if system is not None:
            text = "".join(b.text for b in system.content if isinstance(b, Text))
        self.system_texts.append(text)
        self.tool_names.append([t.name for t in tools])
        self.message_snapshots.append(list(messages))
        idx = self._idx
        self._idx += 1
        if idx >= len(self._scripted):
            return
        for ev in self._scripted[idx]:
            yield ev

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        del model, thinking_budget
        return int(fake_provider_prompt_tokens(messages, tools))


class ToolsRecordingProvider(FakeProvider):
    """FakeProvider that records tool names passed to each ``stream`` call."""

    def __init__(self, scripted: list[list[object]]) -> None:
        super().__init__(scripted)
        self.stream_tools: list[list[str]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        self.stream_tools.append([t.name for t in tools])
        async for ev in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        ):
            yield ev
