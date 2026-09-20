"""Resolve attachmentRef blocks to Image/File/Text for provider calls."""

from __future__ import annotations

import base64
import copy
import threading
import weakref
from collections import OrderedDict
from collections.abc import Sequence

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    Text,
)
from monkeybot.core.types.interfaces import MonkeybotError

from .config import IMAGE_MIME_TYPES, preview_max_bytes, preview_max_dim
from .image_preview import ImagePreviewError, make_provider_preview, provider_preview_metadata
from .store import AttachmentStore

_PREVIEW_CACHE_MAX = 128
_PREVIEW_CACHE_MAX_BYTES = 16 * 1024 * 1024
_PreviewKey = tuple[str, str, str, int, int]
_PreviewBucket = OrderedDict[_PreviewKey, tuple[bytes, str]]
_preview_cache: weakref.WeakKeyDictionary[object, _PreviewBucket] = weakref.WeakKeyDictionary()
_preview_cache_lock = threading.Lock()


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


def _cached_preview(
    store: AttachmentStore,
    session_id: str,
    attachment_id: str,
    raw: bytes,
    mime: str,
) -> tuple[bytes, str]:
    max_dim = preview_max_dim()
    max_bytes = preview_max_bytes()
    key = (session_id, attachment_id, mime, max_dim, max_bytes)
    with _preview_cache_lock:
        bucket = _preview_bucket(store, create=False)
        if bucket is not None and key in bucket:
            bucket.move_to_end(key)
            return bucket[key]

    # Encoding stays outside the lock.
    value = make_provider_preview(raw, mime, max_dim=max_dim, max_bytes=max_bytes)
    with _preview_cache_lock:
        bucket = _preview_bucket(store, create=True)
        if bucket is not None:
            bucket[key] = value
            bucket.move_to_end(key)
            _evict_preview_overflow(bucket)
    return value


def _ref_to_media(
    store: AttachmentStore,
    session_id: str,
    ref: AttachmentRef,
) -> Image | File | Text:
    try:
        raw, mime, stored_filename = store.read(session_id, ref.attachment_id)
    except FileNotFoundError as exc:
        raise AttachmentResolveError(str(exc)) from exc
    mime_use = ref.mime_type or mime
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
        preview_meta = provider_preview_metadata(
            meta,
            original_mime=mime_use,
            preview_mime=preview_mime,
        )
        data_b64 = base64.b64encode(preview_bytes).decode("ascii")
        return Image(mime_type=preview_mime, data=data_b64, metadata=preview_meta)
    if mime_use == "text/plain":
        filename = meta.get("filename")
        label = (
            filename.strip()
            if isinstance(filename, str) and filename.strip()
            else stored_filename
        )
        body = raw.decode("utf-8", errors="replace")
        if label:
            return Text(text=f"[Attached file: {label}]\n{body}")
        return Text(text=body)
    data_b64 = base64.b64encode(raw).decode("ascii")
    return File(mime_type=mime_use, data=data_b64, metadata=meta)


def _resolve_user_content(
    blocks: list[ContentBlock], store: AttachmentStore, session_id: str
) -> list[ContentBlock]:
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
