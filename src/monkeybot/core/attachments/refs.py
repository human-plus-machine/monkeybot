"""Replace tool-result media with an attachment reference before it is stored.

``load_file`` already saved the bytes and put ``attachment_id`` on the block.
Keeping the base64 in history makes every later load, count, and compaction
pay for pixels the attachment store already has. The reference is resolved
back to those bytes on each provider call, and the end-of-turn freeze turns
it into the same text stub an ``Image`` or ``File`` would have become.
"""

from __future__ import annotations

from collections.abc import Sequence

from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    ToolResponse,
)


def _attachment_id(block: Image | File) -> str | None:
    metadata = block.metadata or {}
    raw = metadata.get("attachment_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _ref_for_media(block: Image | File, attachment_id: str) -> AttachmentRef:
    """Reference that resolves to the same preview the tool just produced.

    ``block.mime_type`` is the original mime when the preview did not change
    the format, and the preview mime (``image/jpeg``) when it did. Either is
    an image mime, so resolution previews the raw file again. A ``File`` keeps
    the base64 length so the size estimate does not change while the bytes
    themselves stay out of the row.
    """
    metadata = dict(block.metadata) if block.metadata else {}
    if isinstance(block, File):
        metadata["base64_chars"] = len(block.data)
    return AttachmentRef(
        attachment_id=attachment_id,
        mime_type=block.mime_type,
        metadata=metadata or None,
    )


def _response_media_to_refs(
    block: ToolResponse,
    store: AttachmentStore,
    session_id: str,
) -> ToolResponse:
    changed = False
    result: list[ContentBlock] = []
    for item in block.result:
        if not isinstance(item, (Image, File)):
            result.append(item)
            continue
        attachment_id = _attachment_id(item)
        if attachment_id is None or not store.exists(session_id, attachment_id):
            result.append(item)
            continue
        result.append(_ref_for_media(item, attachment_id))
        changed = True
    if not changed:
        return block
    return ToolResponse(
        id=block.id,
        tool_name=block.tool_name,
        result=result,
        is_error=block.is_error,
    )


def tool_media_to_refs(
    blocks: Sequence[ContentBlock],
    *,
    attachment_store: AttachmentStore | None,
    session_id: str,
) -> list[ContentBlock]:
    """Swap stored ``Image`` and ``File`` blocks for ``AttachmentRef``.

    A block stays as base64 when no store is configured, the block has no
    ``attachment_id``, or that id is not in the store. The live tool event
    is built earlier from the original blocks, so this only changes the row
    that is appended.
    """
    if attachment_store is None:
        return list(blocks)
    out: list[ContentBlock] = []
    for block in blocks:
        if isinstance(block, ToolResponse) and not block.is_error:
            out.append(_response_media_to_refs(block, attachment_store, session_id))
        else:
            out.append(block)
    return out
