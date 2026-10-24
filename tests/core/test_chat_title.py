"""Title cleanup and the short prompt sent to the summarization model."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta
from monkeybot.core.runtime.chat_title import (
    TITLE_MAX_CHARS,
    TITLE_SOURCE_CHARS,
    clean_title,
    clip_title_source,
    generate_chat_title,
    opening_turn_texts,
    title_model_id,
)
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Fixing the login bug."', "Fixing the login bug"),
        ("Title: **Hello world.**", "Hello world"),
        ("  title:   spaced   out  ", "spaced out"),
        ("First line\nignore the rest", "First line"),
        ("...", None),
        ("   ", None),
        ("", None),
        ("Title:", None),
    ],
)
def test_clean_title(raw: str, expected: str | None) -> None:
    assert clean_title(raw) == expected


def test_clean_title_caps_length() -> None:
    title = clean_title("word " * 40)
    assert title is not None
    assert len(title) <= TITLE_MAX_CHARS


def test_clip_title_source_caps_a_long_message() -> None:
    clipped = clip_title_source("u" * (TITLE_SOURCE_CHARS + 40))
    assert clipped == "u" * TITLE_SOURCE_CHARS


def test_opening_turn_skips_an_assistant_message_that_has_no_words() -> None:
    messages = [
        Message(role="user", content=[Text(text="why does the build fail")]),
        Message(role="assistant", content=[Text(text="   ")]),
        Message(role="assistant", content=[Text(text="The import path is wrong.")]),
    ]
    assert opening_turn_texts(messages) == (
        "why does the build fail",
        "The import path is wrong.",
    )


class _RecordingProvider(ScriptedFakeProvider):
    def __init__(self, events: list[ProviderEvent]) -> None:
        super().__init__(events)
        self.prompts: list[str] = []
        self.models: list[str] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.models.append(model)
        user = messages[-1]
        block = user.content[0]
        assert isinstance(block, Text)
        self.prompts.append(block.text)
        async for event in super().stream(
            messages, tools, model=model, thinking_budget=thinking_budget
        ):
            yield event


@pytest.mark.asyncio
async def test_generate_chat_title_clips_inputs_and_cleans_the_reply() -> None:
    provider = _RecordingProvider([TextDelta(text='"Fixing the login bug."'), Done()])
    title = await generate_chat_title(
        provider,
        "small-model",
        "u" * (TITLE_SOURCE_CHARS + 25),
        "a" * (TITLE_SOURCE_CHARS + 25),
    )
    assert title == "Fixing the login bug"
    assert provider.models == ["small-model"]
    [prompt] = provider.prompts
    assert "u" * TITLE_SOURCE_CHARS in prompt
    assert "u" * (TITLE_SOURCE_CHARS + 1) not in prompt
    assert "a" * TITLE_SOURCE_CHARS in prompt
    assert "a" * (TITLE_SOURCE_CHARS + 1) not in prompt


def test_title_model_id_prefers_the_summarization_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "monkeybot.core.runtime.chat_title.current_env",
        lambda key, default="": "cheap-model" if key == "CONTEXT_SUMMARIZATION_MODEL" else "",
    )
    assert title_model_id() == "cheap-model"


def test_title_model_id_falls_back_to_the_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "monkeybot.core.runtime.chat_title.current_env",
        lambda key, default="": "",
    )
    monkeypatch.setattr(
        "monkeybot.core.runtime.chat_title._agent_model_name",
        lambda _cfg, model_name=None: "gemini-2.5-flash",
    )
    assert title_model_id() == "gemini-2.5-flash"
