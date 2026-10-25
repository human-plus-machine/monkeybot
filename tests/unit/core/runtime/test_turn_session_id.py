"""A turn whose history thread differs from its chat session (a conversation branch)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, UsageEvent
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import AttachmentRef, Image, Text
from monkeybot.core.types.types_tools import ToolDef

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
    b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _ctx(*, thread_id: str, session_id: str | None = None) -> TurnContext:
    return TurnContext(
        thread_id=thread_id,
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        session=session_id,
    )


class _History:
    def __init__(self) -> None:
        self.rows: list[Message] = []
        self.threads: set[str] = set()

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        del limit
        self.threads.add(thread_id)
        return list(self.rows)

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del turn_id, message_id
        self.threads.add(thread_id)
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        self.threads.add(thread_id)
        self.rows = list(messages)


class _RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

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
        del tools, model, thinking_budget
        self.requests.append(list(messages))
        yield TextDelta(text="A red pixel.")
        yield UsageEvent(input_tokens=1, output_tokens=1)
        yield Done()

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        del messages, tools, model, thinking_budget
        return 10


class _NoTools:
    async def execute(self, *, call: Any, ctx: TurnContext) -> ToolExecutionResult:
        del call, ctx
        return ToolExecutionResult.ok_text("ok")


def test_session_id_defaults_to_thread_id() -> None:
    assert _ctx(thread_id="s1").session_id == "s1"
    assert _ctx(thread_id="branch:s1:b1", session_id="s1").session_id == "s1"
    assert replace(_ctx(thread_id="s1"), thread_id="s2").session_id == "s2"


@pytest.mark.asyncio
async def test_branch_thread_turn_resolves_session_attachments(tmp_path: Path) -> None:
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save("s1", data=_PNG, mime_type="image/png", filename="dot.png")
    history = _History()
    provider = _RecordingProvider()
    catalog = SessionAttachmentCatalog(session_id="s1")

    async for _ in run(
        [
            Text(text="what is this?"),
            AttachmentRef(attachment_id=stored.attachment_id, mime_type="image/png"),
        ],
        _ctx(thread_id="branch:s1:b1", session_id="s1"),
        provider=provider,
        history=history,
        inspectors=[],
        tool_executor=_NoTools(),
        max_turns=2,
        attachment_store=store,
        attachment_catalog=catalog,
    ):
        pass

    assert history.threads == {"branch:s1:b1"}
    sent = [block for message in provider.requests[0] for block in message.content]
    assert any(isinstance(block, Image) for block in sent)
    records = catalog.list_records()
    assert [record.storage_path for record in records] == [
        f".monkeybot/attachments/s1/{stored.attachment_id}"
    ]
