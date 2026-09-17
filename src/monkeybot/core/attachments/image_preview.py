"""Bounded provider-facing image previews. Originals stay on disk."""

from __future__ import annotations

import io
import logging
from pathlib import PurePath
from typing import Any

from monkeybot.core.attachments.config import IMAGE_MIME_TYPES, preview_max_bytes, preview_max_dim
from monkeybot.core.logging_utils import kv

logger = logging.getLogger(__name__)

_JPEG_MIME = "image/jpeg"
_JPEG_QUALITY_LADDER = (60, 40, 30, 20)
_MIN_PREVIEW_DIM = 64
_DIMENSION_SCALE = 0.75


class ImagePreviewError(ValueError):
    """An image could not be converted into a provider-safe preview."""


def provider_preview_metadata(
    metadata: dict[str, Any] | None,
    *,
    original_mime: str,
    preview_mime: str,
) -> dict[str, Any] | None:
    """Keep filename metadata consistent when a preview changes image format."""
    if not metadata or preview_mime == original_mime:
        return dict(metadata) if metadata else None
    out = dict(metadata)
    filename = out.get("filename")
    if isinstance(filename, str) and filename:
        out.setdefault("original_filename", filename)
        out["filename"] = f"{PurePath(filename).stem}.jpg"
    return out


def _rgb_first_frame(opened: Any) -> Any:
    """Apply EXIF orientation and composite transparency onto white."""
    from PIL import Image, ImageOps  # noqa: PLC0415

    opened.seek(0)
    transposed = ImageOps.exif_transpose(opened)
    transposed.load()
    try:
        if "A" in transposed.getbands():
            rgba = transposed.convert("RGBA")
            try:
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                return rgb
            finally:
                rgba.close()
        return transposed.convert("RGB")
    finally:
        if transposed is not opened:
            transposed.close()


def make_provider_preview(
    raw: bytes,
    mime: str,
    *,
    max_bytes: int | None = None,
    max_dim: int | None = None,
) -> tuple[bytes, str]:
    """Return provider-safe ``(bytes, mime)`` without ever returning oversized input."""
    if mime not in IMAGE_MIME_TYPES:
        return raw, mime
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - Pillow is a runtime dependency
        raise ImagePreviewError("Pillow is required to prepare image previews") from exc

    byte_cap = preview_max_bytes() if max_bytes is None else max_bytes
    dimension_cap = preview_max_dim() if max_dim is None else max_dim
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            width, height = opened.size
            if len(raw) <= byte_cap and width <= dimension_cap and height <= dimension_cap:
                return raw, mime
            if bool(getattr(opened, "is_animated", False)):
                logger.warning("animated image preview uses the first frame")
            working = _rgb_first_frame(opened)
    except (OSError, ValueError) as exc:
        raise ImagePreviewError("image preview decode failed") from exc

    try:
        dimension = min(dimension_cap, max(working.size))
        smallest_bytes = 0
        while True:
            candidate = working.copy()
            candidate.thumbnail((dimension, dimension))
            try:
                for quality in _JPEG_QUALITY_LADDER:
                    buf = io.BytesIO()
                    candidate.save(
                        buf,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=False,
                    )
                    payload = buf.getvalue()
                    if not smallest_bytes or len(payload) < smallest_bytes:
                        smallest_bytes = len(payload)
                    if len(payload) <= byte_cap:
                        return payload, _JPEG_MIME
            finally:
                candidate.close()
            if dimension <= _MIN_PREVIEW_DIM:
                break
            dimension = max(_MIN_PREVIEW_DIM, int(dimension * _DIMENSION_SCALE))
        logger.error(
            "image preview cannot fit byte cap %s",
            kv(bytes=smallest_bytes, max_bytes=byte_cap, min_dim=_MIN_PREVIEW_DIM),
        )
        raise ImagePreviewError(
            f"image preview exceeds provider byte cap ({smallest_bytes} > {byte_cap})"
        )
    finally:
        working.close()
