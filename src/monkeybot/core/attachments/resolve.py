"""Resolve attachmentRef blocks to Image/File for provider calls."""

from __future__ import annotations

import copy
from collections.abc import Sequence

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    Text,
    ToolResponse,
)
from monkeybot.core.types.interfaces import MonkeybotError

from .config import IMAGE_MIME_TYPES
from .store import AttachmentStore


class AttachmentResolveError(MonkeybotError):
    """Failed to load attachment bytes for provider resolution."""


def _ref_to_media(
    store: AttachmentStore,
    session_id: str,
    ref: AttachmentRef,
) -> Image | File:
    try:
        data_b64, mime, _filename = store.read_base64(session_id, ref.attachment_id)
    except FileNotFoundError as exc:
        raise AttachmentResolveError(str(exc)) from exc
    mime_use = ref.mime_type or mime
    meta = dict(ref.metadata) if ref.metadata else None
    if mime_use in IMAGE_MIME_TYPES:
        return Image(mime_type=mime_use, data=data_b64, metadata=meta)
    return File(mime_type=mime_use, data=data_b64, metadata=meta)


def _resolve_user_content(blocks: list[ContentBlock], store: AttachmentStore, session_id: str) -> list[ContentBlock]:
    out: list[ContentBlock] = []
    for block in blocks:
        if isinstance(block, AttachmentRef):
            out.append(_ref_to_media(store, session_id, block))
        else:
            out.append(block)
    return out


def resolve_messages_for_provider(
    messages: Sequence[Message],
    *,
    attachment_store: AttachmentStore | None,
    session_id: str,
) -> list[Message]:
    """Return a copy of messages with live attachmentRef rows resolved to Image/File."""
    if attachment_store is None:
        return list(messages)

    resolved: list[Message] = []
    for msg in copy.deepcopy(list(messages)):
        if msg.role != "user" or not any(isinstance(b, AttachmentRef) for b in msg.content):
            resolved.append(msg)
            continue
        new_content = _resolve_user_content(list(msg.content), attachment_store, session_id)
        resolved.append(Message(role=msg.role, content=new_content))
    return resolved
