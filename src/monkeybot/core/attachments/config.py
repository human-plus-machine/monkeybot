"""Attachment feature flags and limits from environment."""

from __future__ import annotations

import os

ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/html",
        "text/markdown",
        "text/plain",
    }
)

IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    t for t in ALLOWED_MIME_TYPES if t.startswith("image/")
)

# Decoded to Text at resolve. HTML stays a File so the Files preview can render it.
INLINE_TEXT_MIME_TYPES: frozenset[str] = frozenset({"text/plain", "text/markdown"})

_MIME_ALIASES: dict[str, str] = {"text/x-markdown": "text/markdown"}
# Browsers often send an empty type for .md (Windows especially).
_GENERIC_MIME_TYPES: frozenset[str] = frozenset({"", "application/octet-stream"})
_MIME_BY_SUFFIX: dict[str, str] = {".md": "text/markdown", ".markdown": "text/markdown"}


def normalize_upload_mime(content_type: str | None, filename: str) -> str:
    """Canonical mime for an upload: drop params, map aliases, infer generic types from suffix."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if mime in _GENERIC_MIME_TYPES:
        suffix = os.path.splitext(filename)[1].lower()
        mime = _MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
    return mime


def attachments_enabled_from_env() -> bool:
    raw = os.getenv("ATTACHMENTS_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def max_image_bytes() -> int:
    return _env_int("ATTACHMENT_MAX_IMAGE_BYTES", 20 * 1024 * 1024)


def max_pdf_bytes() -> int:
    return _env_int("ATTACHMENT_MAX_PDF_BYTES", 50 * 1024 * 1024)


def preview_max_dim() -> int:
    return max(64, _env_int("ATTACHMENT_PREVIEW_MAX_DIM", 1200))


def preview_max_bytes() -> int:
    return max(8_192, _env_int("ATTACHMENT_PREVIEW_MAX_BYTES", 500_000))


def max_attachments_per_session() -> int:
    return max(1, _env_int("ATTACHMENT_MAX_PER_SESSION", 50))


def max_attachments_per_reply() -> int:
    return max(1, _env_int("ATTACHMENT_MAX_PER_REPLY", 5))


def attachment_ttl_hours() -> int:
    return max(1, _env_int("ATTACHMENT_TTL_HOURS", 48))
