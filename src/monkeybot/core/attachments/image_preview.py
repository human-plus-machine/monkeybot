"""Bounded provider-facing image previews. Originals stay on disk."""

from __future__ import annotations

import io
import logging

from monkeybot.core.attachments.config import IMAGE_MIME_TYPES, preview_max_bytes, preview_max_dim
from monkeybot.core.logging_utils import kv

logger = logging.getLogger(__name__)

_JPEG_MIME = "image/jpeg"
_JPEG_QUALITY_LADDER = (60, 40, 30, 20)


def make_provider_preview(raw: bytes, mime: str) -> tuple[bytes, str]:
    """Return provider-safe ``(bytes, mime)``; originals are unchanged if already small."""
    if mime not in IMAGE_MIME_TYPES:
        return raw, mime
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("pillow not installed; sending original image bytes to the provider")
        return raw, mime

    max_bytes = preview_max_bytes()
    max_dim = preview_max_dim()
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            img = ImageOps.exif_transpose(opened) or opened
            img.load()
            working = img.convert("RGB") if img.mode not in {"RGB", "L"} else img.copy()
    except Exception:
        logger.warning("image preview decode failed; sending original bytes", exc_info=True)
        return raw, mime

    try:
        width, height = working.size
        if len(raw) <= max_bytes and width <= max_dim and height <= max_dim:
            return raw, mime
        working.thumbnail((max_dim, max_dim))
        rgb = working.convert("RGB")
        try:
            payload = b""
            for quality in _JPEG_QUALITY_LADDER:
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
                payload = buf.getvalue()
                if len(payload) <= max_bytes:
                    return payload, _JPEG_MIME
            logger.warning(
                "image preview still over byte cap %s",
                kv(bytes=len(payload), max_bytes=max_bytes),
            )
            return payload, _JPEG_MIME
        finally:
            if rgb is not working:
                rgb.close()
    finally:
        working.close()
