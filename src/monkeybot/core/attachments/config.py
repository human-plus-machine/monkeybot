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
    }
)

IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    t for t in ALLOWED_MIME_TYPES if t.startswith("image/")
)


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


def max_attachments_per_session() -> int:
    return max(1, _env_int("ATTACHMENT_MAX_PER_SESSION", 50))


def max_attachments_per_reply() -> int:
    return max(1, _env_int("ATTACHMENT_MAX_PER_REPLY", 5))


def attachment_ttl_hours() -> int:
    return max(1, _env_int("ATTACHMENT_TTL_HOURS", 48))
