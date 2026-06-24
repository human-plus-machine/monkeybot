"""Freeze attachment refs and tool-result media to Text in history."""

from __future__ import annotations

from collections.abc import Sequence

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.runtime.events import AttachmentDescriptorEvent
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    Text,
    ToolResponse,
)

from .catalog import AttachmentRecord, SessionAttachmentCatalog
from .text import (
    filename_from_metadata,
    render_attachment_descriptor_text,
    render_tool_media_freeze_text,
)


def _freeze_user_row(
    msg: Message,
    *,
    thread_id: str,
    last_assistant_text: str,
    catalog: SessionAttachmentCatalog | None,
    events: list[AttachmentDescriptorEvent],
) -> Message:
    user_text = " ".join(
        b.text.strip() for b in msg.content if isinstance(b, Text) and b.text.strip()
    )
    new_content: list[ContentBlock] = []
    for block in msg.content:
        if isinstance(block, Text):
            new_content.append(block)
            continue
        if not isinstance(block, AttachmentRef):
            new_content.append(block)
            continue
        filename = filename_from_metadata(block.metadata, fallback=block.attachment_id)
        description = last_assistant_text.strip() or user_text
        if not description:
            description = f"{filename} ({block.mime_type}) attached by user."
        frozen_text = render_attachment_descriptor_text(
            attachment_id=block.attachment_id,
            mime_type=block.mime_type,
            filename=filename,
            description=description,
        )
        new_content.append(Text(text=frozen_text))
        events.append(
            AttachmentDescriptorEvent(
                attachment_id=block.attachment_id,
                mime_type=block.mime_type,
                filename=filename,
                description=description[:500],
            )
        )
        if catalog is not None:
            catalog.upsert(
                AttachmentRecord(
                    attachment_id=block.attachment_id,
                    filename=filename,
                    mime_type=block.mime_type,
                    description=description[:500],
                    storage_path=f".monkeybot/attachments/{thread_id}/{block.attachment_id}",
                )
            )
    return Message(role=msg.role, content=new_content)


def _freeze_tool_responses(msg: Message) -> Message:
    new_content: list[ContentBlock] = []
    changed = False
    for block in msg.content:
        if not isinstance(block, ToolResponse):
            new_content.append(block)
            continue
        if not any(isinstance(b, (Image, File)) for b in block.result):
            new_content.append(block)
            continue
        kind = "image" if any(isinstance(b, Image) for b in block.result) else "file"
        if any(
            isinstance(b, File) and b.mime_type == "application/pdf" for b in block.result
        ):
            kind = "pdf"
        summary = render_tool_media_freeze_text(
            tool_name=block.tool_name,
            attachment_id=None,
            kind=kind,
        )
        changed = True
        new_content.append(
            ToolResponse(
                id=block.id,
                tool_name=block.tool_name,
                result=[Text(text=summary)],
                is_error=block.is_error,
            )
        )
    if not changed:
        return msg
    return Message(role=msg.role, content=new_content)


async def freeze_attachments_in_history(
    *,
    thread_id: str,
    history: HistoryStore,
    catalog: SessionAttachmentCatalog | None,
    last_assistant_text: str,
) -> list[AttachmentDescriptorEvent]:
    """Rewrite user attachmentRef rows and tool-result media to Text; return SSE events."""
    rows = await history.load(thread_id)
    mutated: list[Message] = []
    events: list[AttachmentDescriptorEvent] = []
    changed = False

    for msg in rows:
        frozen_msg = msg
        if msg.role == "user" and any(isinstance(b, AttachmentRef) for b in msg.content):
            frozen_msg = _freeze_user_row(
                msg,
                thread_id=thread_id,
                last_assistant_text=last_assistant_text,
                catalog=catalog,
                events=events,
            )
            changed = True
        tool_frozen = _freeze_tool_responses(frozen_msg)
        if tool_frozen is not frozen_msg:
            changed = True
        mutated.append(tool_frozen)

    if changed:
        await history.reset(thread_id, mutated)
    return events


def ensure_catalog_from_history(
    catalog: SessionAttachmentCatalog,
    messages: Sequence[Message],
) -> None:
    catalog.rebuild_from_history(messages)
