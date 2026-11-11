"""Resolve attachmentRef blocks to Image/File/Text for provider calls."""

from __future__ import annotations

import base64
import copy
import logging
import threading
import weakref
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    Text,
    ToolResponse,
)
from monkeybot.core.types.interfaces import MonkeybotError

from .config import IMAGE_MIME_TYPES, INLINE_TEXT_MIME_TYPES, preview_max_bytes, preview_max_dim
from .image_preview import ImagePreviewError, make_provider_preview, provider_preview_metadata
from .store import AttachmentStore

logger = logging.getLogger(__name__)

_PREVIEW_CACHE_MAX = 128
_PREVIEW_CACHE_MAX_BYTES = 16 * 1024 * 1024
_PreviewKey = tuple[str, str, str, int, int]
_PreviewBucket = OrderedDict[_PreviewKey, tuple[bytes, str]]
_preview_cache: weakref.WeakKeyDictionary[object, _PreviewBucket] = weakref.WeakKeyDictionary()
_preview_cache_lock = threading.Lock()

# One request resolves the same attachment on every convert. The key is the
# attachment, the mime the ref declared, and the preview caps in force.
MediaMemoKey = tuple[str, str, int, int]
MediaMemo = dict[MediaMemoKey, Image | File | Text]


class AttachmentResolveError(MonkeybotError):
    """Failed to load attachment bytes for provider resolution."""


def _preview_bucket(store: AttachmentStore, *, create: bool) -> _PreviewBucket | None:
    """Bucket for *store*, or None for stores that cannot be weak-referenced or hashed."""
    try:
        bucket = _preview_cache.get(store)
        if bucket is None and create:
            bucket = OrderedDict()
            _preview_cache[store] = bucket
        return bucket
    except TypeError:
        return None


def _evict_preview_overflow(bucket: _PreviewBucket) -> None:
    total = sum(len(data) for data, _mime in bucket.values())
    while bucket and (len(bucket) > _PREVIEW_CACHE_MAX or total > _PREVIEW_CACHE_MAX_BYTES):
        _key, (data, _mime) = bucket.popitem(last=False)
        total -= len(data)


def _preview_key(session_id: str, attachment_id: str, mime: str) -> _PreviewKey:
    return (session_id, attachment_id, mime, preview_max_dim(), preview_max_bytes())


def _lookup_preview(
    store: AttachmentStore,
    session_id: str,
    attachment_id: str,
    mime: str,
) -> tuple[bytes, str] | None:
    key = _preview_key(session_id, attachment_id, mime)
    with _preview_cache_lock:
        bucket = _preview_bucket(store, create=False)
        if bucket is None or key not in bucket:
            return None
        bucket.move_to_end(key)
        return bucket[key]


def _remember_preview(
    store: AttachmentStore,
    session_id: str,
    attachment_id: str,
    mime: str,
    value: tuple[bytes, str],
) -> None:
    key = _preview_key(session_id, attachment_id, mime)
    with _preview_cache_lock:
        bucket = _preview_bucket(store, create=True)
        if bucket is None:
            return
        bucket[key] = value
        bucket.move_to_end(key)
        _evict_preview_overflow(bucket)


def _cached_preview(
    store: AttachmentStore,
    session_id: str,
    attachment_id: str,
    raw: bytes,
    mime: str,
) -> tuple[bytes, str]:
    found = _lookup_preview(store, session_id, attachment_id, mime)
    if found is not None:
        return found

    # Encoding stays outside the lock.
    value = make_provider_preview(
        raw,
        mime,
        max_dim=preview_max_dim(),
        max_bytes=preview_max_bytes(),
    )
    _remember_preview(store, session_id, attachment_id, mime, value)
    return value


def _memo_key(attachment_id: str, mime: str) -> MediaMemoKey:
    return (attachment_id, mime, preview_max_dim(), preview_max_bytes())


def _remember_memo(
    memo: MediaMemo | None,
    attachment_id: str,
    mime: str,
    block: Image | File | Text,
) -> None:
    if memo is not None and mime:
        memo[_memo_key(attachment_id, mime)] = block


def _image_from_preview(
    ref: AttachmentRef,
    preview_bytes: bytes,
    preview_mime: str,
    *,
    original_mime: str,
) -> Image:
    meta = dict(ref.metadata) if ref.metadata else {}
    meta.setdefault("attachment_id", ref.attachment_id)
    preview_meta = provider_preview_metadata(
        meta,
        original_mime=original_mime,
        preview_mime=preview_mime,
    )
    data_b64 = base64.b64encode(preview_bytes).decode("ascii")
    return Image(mime_type=preview_mime, data=data_b64, metadata=preview_meta)


def _ref_to_media(
    store: AttachmentStore,
    session_id: str,
    ref: AttachmentRef,
    *,
    memo: MediaMemo | None = None,
) -> Image | File | Text:
    hinted = ref.mime_type
    if memo is not None and hinted:
        cached_block = memo.get(_memo_key(ref.attachment_id, hinted))
        if cached_block is not None:
            return cached_block

    # A preview cached for this attachment is the bytes the provider will see.
    # Reading the original first would repeat the disk read on every convert.
    if hinted in IMAGE_MIME_TYPES:
        preview = _lookup_preview(store, session_id, ref.attachment_id, hinted)
        if preview is not None:
            image = _image_from_preview(ref, preview[0], preview[1], original_mime=hinted)
            _remember_memo(memo, ref.attachment_id, hinted, image)
            return image

    try:
        raw, mime, stored_filename = store.read(session_id, ref.attachment_id)
    except FileNotFoundError as exc:
        raise AttachmentResolveError(str(exc)) from exc
    mime_use = hinted or mime
    meta = dict(ref.metadata) if ref.metadata else {}
    meta.setdefault("attachment_id", ref.attachment_id)
    if mime_use in IMAGE_MIME_TYPES:
        try:
            preview_bytes, preview_mime = _cached_preview(
                store, session_id, ref.attachment_id, raw, mime_use
            )
        except ImagePreviewError as exc:
            raise AttachmentResolveError(
                f"Failed to prepare attachment {ref.attachment_id} for the provider: {exc}"
            ) from exc
        image = _image_from_preview(
            ref,
            preview_bytes,
            preview_mime,
            original_mime=mime_use,
        )
        _remember_memo(memo, ref.attachment_id, mime_use, image)
        return image
    if mime_use in INLINE_TEXT_MIME_TYPES:
        filename = meta.get("filename")
        label = (
            filename.strip() if isinstance(filename, str) and filename.strip() else stored_filename
        )
        body = raw.decode("utf-8", errors="replace")
        text = Text(text=f"[Attached file: {label}]\n{body}") if label else Text(text=body)
        _remember_memo(memo, ref.attachment_id, mime_use, text)
        return text
    data_b64 = base64.b64encode(raw).decode("ascii")
    file_block = File(mime_type=mime_use, data=data_b64, metadata=meta)
    _remember_memo(memo, ref.attachment_id, mime_use, file_block)
    return file_block


def _unavailable_tool_media(ref: AttachmentRef, tool_name: str) -> Text:
    return Text(
        text=(
            f"[{tool_name} result no longer available; "
            f'call load_file(attachment_id="{ref.attachment_id}") to reload.]'
        )
    )


def _resolve_tool_response(
    block: ToolResponse,
    store: AttachmentStore,
    session_id: str,
    memo: MediaMemo | None,
) -> ToolResponse:
    if not any(isinstance(item, AttachmentRef) for item in block.result):
        return block
    result: list[ContentBlock] = []
    for item in block.result:
        if not isinstance(item, AttachmentRef):
            result.append(item)
            continue
        try:
            result.append(_ref_to_media(store, session_id, item, memo=memo))
        except AttachmentResolveError as exc:
            logger.warning(
                "tool attachment ref unavailable %s",
                kv(
                    attachment_id=item.attachment_id,
                    tool=block.tool_name,
                    error=str(exc),
                ),
            )
            result.append(_unavailable_tool_media(item, block.tool_name))
    return replace(block, result=result)


def _resolve_content(
    msg: Message,
    store: AttachmentStore,
    session_id: str,
    memo: MediaMemo | None,
) -> list[ContentBlock] | None:
    """Resolved blocks, or None when this message has nothing to resolve."""
    changed = False
    out: list[ContentBlock] = []
    for block in msg.content:
        if isinstance(block, AttachmentRef) and msg.role == "user":
            out.append(_ref_to_media(store, session_id, block, memo=memo))
            changed = True
            continue
        if isinstance(block, ToolResponse):
            resolved = _resolve_tool_response(block, store, session_id, memo)
            if resolved is not block:
                changed = True
            out.append(resolved)
            continue
        out.append(block)
    if not changed:
        return None
    return out


def resolve_messages_for_provider(
    messages: Sequence[Message],
    *,
    attachment_store: AttachmentStore | None,
    session_id: str,
    media_memo: MediaMemo | None = None,
) -> list[Message]:
    """Return a copy of messages with attachment refs resolved to Image/File.

    A ref at the top of a user row still raises ``AttachmentResolveError`` when
    the file is missing. A ref inside a tool result becomes a short stub
    instead, so one expired ``load_file`` does not end the turn.
    """
    if attachment_store is None:
        return list(messages)

    resolved: list[Message] = []
    for msg in copy.deepcopy(list(messages)):
        new_content = _resolve_content(msg, attachment_store, session_id, media_memo)
        if new_content is None:
            resolved.append(msg)
            continue
        resolved.append(replace(msg, content=new_content))
    return resolved
