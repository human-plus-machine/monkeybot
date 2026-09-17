"""Bounded provider-facing image previews keep originals on disk."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest
from PIL import Image as PILImage

from monkeybot.core.attachments.image_preview import make_provider_preview
from monkeybot.core.attachments.resolve import resolve_messages_for_provider
from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import AttachmentRef, Image, Text


def _noisy_png(size: int = 320) -> bytes:
    img = PILImage.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_make_provider_preview_shrinks_large_png(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_DIM", "64")
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_BYTES", "12000")
    raw = _noisy_png(400)
    preview, mime = make_provider_preview(raw, "image/png")
    assert mime in {"image/jpeg", "image/png"}
    assert len(preview) < len(raw)


def test_resolve_uses_preview_not_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_DIM", "64")
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_BYTES", "12000")
    raw = _noisy_png(400)
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save("s1", data=raw, mime_type="image/png", filename="n.png")
    on_disk, _mime, _name = store.read("s1", stored.attachment_id)
    assert on_disk == raw
    msgs = resolve_messages_for_provider(
        [
            Message(
                role="user",
                content=[
                    Text(text="see"),
                    AttachmentRef(
                        attachment_id=stored.attachment_id,
                        mime_type="image/png",
                        metadata={"filename": "n.png"},
                    ),
                ],
            )
        ],
        attachment_store=store,
        session_id="s1",
    )
    img = next(b for b in msgs[0].content if isinstance(b, Image))
    preview = base64.b64decode(img.data)
    assert len(preview) < len(raw)
    still, _m, _n = store.read("s1", stored.attachment_id)
    assert still == raw
