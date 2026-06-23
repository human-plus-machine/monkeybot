"""Normalize and validate POST /reply bodies into user ContentBlock lists."""

from __future__ import annotations

from monkeybot.core.attachments.config import max_attachments_per_reply
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    Text,
)


class ReplyBodyError(ValueError):
    """Validation failure for reply body normalization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_reply_to_user_content(
    *,
    message: str | None,
    content: list[dict[str, object]] | None,
    session_id: str,
    attachment_store: AttachmentStore | None,
) -> list[ContentBlock]:
    has_message = message is not None and message.strip() != ""
    has_content = content is not None and len(content) > 0
    if has_message and has_content:
        raise ReplyBodyError("AMBIGUOUS_REPLY_BODY", "Send either message or content, not both")
    if not has_message and not has_content:
        raise ReplyBodyError("EMPTY_REPLY_BODY", "Reply must include message or content")

    if has_message and not has_content:
        return [Text(text=message or "")]

    blocks: list[ContentBlock] = []
    ref_count = 0
    has_text = False
    for raw in content or []:
        if not isinstance(raw, dict):
            raise ReplyBodyError("INVALID_CONTENT", "content blocks must be JSON objects")
        tag = raw.get("type")
        if tag in ("image", "file") and raw.get("data"):
            raise ReplyBodyError(
                "INLINE_ATTACHMENT_NOT_ALLOWED",
                "Upload attachments via POST /attachments; use attachmentRef in content",
            )
        block = ContentBlock.from_dict(raw)
        if isinstance(block, Image) or isinstance(block, File):
            raise ReplyBodyError(
                "INLINE_ATTACHMENT_NOT_ALLOWED",
                "Upload attachments via POST /attachments; use attachmentRef in content",
            )
        if isinstance(block, Text):
            if block.text.strip():
                has_text = True
            blocks.append(block)
        elif isinstance(block, AttachmentRef):
            ref_count += 1
            if attachment_store is None or not attachment_store.exists(
                session_id, block.attachment_id
            ):
                raise ReplyBodyError(
                    "ATTACHMENT_NOT_FOUND",
                    f"Unknown attachment_id: {block.attachment_id}",
                )
            blocks.append(block)
        else:
            raise ReplyBodyError(
                "INVALID_CONTENT",
                f"Unsupported content block type for /reply: {type(block).__name__}",
            )

    if ref_count > max_attachments_per_reply():
        raise ReplyBodyError(
            "TOO_MANY_ATTACHMENTS",
            f"At most {max_attachments_per_reply()} attachments per reply",
        )
    if ref_count == 0 and not has_text:
        raise ReplyBodyError(
            "EMPTY_REPLY_BODY",
            "Reply content must include text or at least one attachmentRef",
        )
    if not blocks:
        raise ReplyBodyError("EMPTY_REPLY_BODY", "Reply content is empty")
    return blocks
