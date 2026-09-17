"""Bounded provider-facing image previews keep originals on disk."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest
from PIL import Image as PILImage

from monkeybot.core.attachments.image_preview import ImagePreviewError, make_provider_preview
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


def test_make_provider_preview_reduces_dimensions_to_meet_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_DIM", "256")
    preview, mime = make_provider_preview(
        _noisy_png(400),
        "image/png",
        max_bytes=4_000,
    )
    assert mime == "image/jpeg"
    assert len(preview) <= 4_000
    with PILImage.open(io.BytesIO(preview)) as image:
        assert max(image.size) < 256


def test_make_provider_preview_composites_alpha_on_white(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_DIM", "64")
    transparent = PILImage.new("RGBA", (200, 200), (255, 0, 0, 0))
    raw_buf = io.BytesIO()
    transparent.save(raw_buf, format="PNG")
    preview, mime = make_provider_preview(raw_buf.getvalue(), "image/png")
    assert mime == "image/jpeg"
    with PILImage.open(io.BytesIO(preview)) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))
    assert min(red, green, blue) > 240


def test_make_provider_preview_fails_closed_on_invalid_image() -> None:
    with pytest.raises(ImagePreviewError, match="decode failed"):
        make_provider_preview(b"not an image", "image/png")


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
    assert img.mime_type == "image/jpeg"
    assert img.metadata == {
        "filename": "n.jpg",
        "original_filename": "n.png",
        "attachment_id": stored.attachment_id,
    }
    still, _m, _n = store.read("s1", stored.attachment_id)
    assert still == raw


def test_resolve_caches_preview_per_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.attachments import resolve

    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_DIM", "64")
    raw = _noisy_png(400)
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save("s1", data=raw, mime_type="image/png", filename="n.png")
    message = Message(
        role="user",
        content=[
            AttachmentRef(
                attachment_id=stored.attachment_id,
                mime_type="image/png",
            )
        ],
    )
    real_make = resolve.make_provider_preview
    calls = 0

    def _counted(*args: object, **kwargs: object) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        return real_make(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(resolve, "make_provider_preview", _counted)
    resolve_messages_for_provider([message], attachment_store=store, session_id="s1")
    resolve_messages_for_provider([message], attachment_store=store, session_id="s1")
    assert calls == 1
