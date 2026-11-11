"""Freeze attachment refs and tool-result media to Text in history."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from monkeybot.core.attachments.config import IMAGE_MIME_TYPES
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
from .store import attachment_workspace_path
from .text import (
    filename_from_metadata,
    render_attachment_descriptor_text,
    render_tool_media_freeze_text,
)


def _freeze_user_row(
    msg: Message,
    *,
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
                    storage_path=attachment_workspace_path(catalog.session_id, block.attachment_id),
                )
            )
    return replace(msg, content=new_content)


def _is_tool_media(block: ContentBlock) -> bool:
    return isinstance(block, (Image, File, AttachmentRef))


def _is_image_media(block: ContentBlock) -> bool:
    if isinstance(block, Image):
        return True
    return isinstance(block, AttachmentRef) and block.mime_type in IMAGE_MIME_TYPES


def _is_pdf_media(block: ContentBlock) -> bool:
    return isinstance(block, (File, AttachmentRef)) and block.mime_type == "application/pdf"


def _media_locator(block: ContentBlock) -> tuple[str | None, str | None]:
    """``(attachment_id, path)`` carried by a tool image, file, or reference."""
    if isinstance(block, AttachmentRef):
        attachment_id = block.attachment_id.strip() or None
        meta = block.metadata or {}
    elif isinstance(block, (Image, File)):
        meta = block.metadata or {}
        raw_id = meta.get("attachment_id")
        attachment_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
    else:
        return None, None
    raw_path = meta.get("path")
    path = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else None
    return attachment_id, path


def _freeze_tool_responses(msg: Message) -> Message:
    new_content: list[ContentBlock] = []
    changed = False
    for block in msg.content:
        if not isinstance(block, ToolResponse):
            new_content.append(block)
            continue
        if not any(_is_tool_media(b) for b in block.result):
            new_content.append(block)
            continue
        # A pdf wins over an image when one result holds both, matching the
        # previous order: image first, then a pdf overwrites the kind.
        kind = "image" if any(_is_image_media(b) for b in block.result) else "file"
        if any(_is_pdf_media(b) for b in block.result):
            kind = "pdf"
        attachment_id: str | None = None
        path: str | None = None
        for b in block.result:
            found_id, found_path = _media_locator(b)
            if attachment_id is None:
                attachment_id = found_id
            if path is None:
                path = found_path
            if attachment_id is not None and path is not None:
                break
        summary = render_tool_media_freeze_text(
            tool_name=block.tool_name,
            attachment_id=attachment_id,
            kind=kind,
            path=path,
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
    return replace(msg, content=new_content)


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
