"""Tests for attachment freeze after turn completion."""

from __future__ import annotations

import pytest

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import AttachmentRef, Image, Text, ToolResponse


class _InMemoryHistory:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        self.reset_calls = 0

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        _ = (thread_id, limit)
        return list(self._messages)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        _ = thread_id
        self.reset_calls += 1
        self._messages = list(messages)


@pytest.mark.asyncio
async def test_freeze_replaces_attachment_ref_without_mutating_frozen_message() -> None:
    user_msg = Message(
        role="user",
        content=[
            Text(text="what is this?"),
            AttachmentRef(
                attachment_id="att_abc",
                mime_type="image/png",
                metadata={"filename": "photo.png"},
            ),
        ],
    )
    assistant_msg = Message.text("assistant", "A red circle on white background.")
    history = _InMemoryHistory([user_msg, assistant_msg])
    catalog = SessionAttachmentCatalog(session_id="thread-1")

    events = await freeze_attachments_in_history(
        thread_id="thread-1",
        history=history,
        catalog=catalog,
        last_assistant_text="A red circle on white background.",
    )

    assert history.reset_calls == 1
    assert len(events) == 1
    assert events[0].attachment_id == "att_abc"

    frozen_user = history._messages[0]
    assert frozen_user.role == "user"
    assert len(frozen_user.content) == 2
    assert isinstance(frozen_user.content[0], Text)
    assert isinstance(frozen_user.content[1], Text)
    assert "att_abc" in frozen_user.content[1].text
    assert catalog.get("att_abc") is not None


@pytest.mark.asyncio
async def test_freeze_tool_result_media_includes_attachment_id() -> None:
    tool_msg = Message(
        role="user",
        content=[
            ToolResponse(
                id="call_1",
                tool_name="render_image",
                result=[
                    Image(
                        data="aW1n",
                        mime_type="image/png",
                        metadata={"attachment_id": "att_gen1", "filename": "out.png"},
                    )
                ],
            )
        ],
    )
    history = _InMemoryHistory([tool_msg])

    await freeze_attachments_in_history(
        thread_id="thread-1",
        history=history,
        catalog=None,
        last_assistant_text="",
    )

    frozen = history._messages[0].content[0]
    assert isinstance(frozen, ToolResponse)
    assert isinstance(frozen.result[0], Text)
    assert "att_gen1" in frozen.result[0].text
    assert 'read_attachment("att_gen1")' in frozen.result[0].text


@pytest.mark.asyncio
async def test_freeze_replaces_tool_result_media() -> None:
    tool_msg = Message(
        role="assistant",
        content=[
            ToolResponse(
                id="call_1",
                tool_name="read_attachment",
                result=[Image(data="aW1n", mime_type="image/png")],
            )
        ],
    )
    history = _InMemoryHistory([tool_msg])

    await freeze_attachments_in_history(
        thread_id="thread-1",
        history=history,
        catalog=None,
        last_assistant_text="",
    )

    frozen = history._messages[0].content[0]
    assert isinstance(frozen, ToolResponse)
    assert len(frozen.result) == 1
    assert isinstance(frozen.result[0], Text)
    assert "read_attachment" in frozen.result[0].text
