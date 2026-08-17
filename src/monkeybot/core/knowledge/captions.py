"""Image captions for knowledge indexing (path stub + optional LLM vision)."""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from monkeybot.core.knowledge.extractors import IMAGE_SUFFIXES, content_hash
from monkeybot.core.knowledge.types import CaptionMode

logger = logging.getLogger(__name__)

VisionCaptionFn = Callable[[Path, str], Awaitable[str | None]]

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_CAPTION_PROMPT = (
    "Describe this image in one or two short sentences for search indexing. "
    "Focus on subject, UI/layout if any, and notable text visible in the image. "
    "No markdown."
)


def path_caption(rel_path: str) -> str:
    """Deterministic FTS-friendly caption from relative path + stem."""
    cleaned = rel_path.strip().lstrip("./")
    name = Path(cleaned).name
    stem = Path(name).stem
    return f"Image: {cleaned} ({stem})"


def _cache_path(cache_dir: Path, digest: str) -> Path:
    return cache_dir / f"{digest}.txt"


def read_cached_caption(cache_dir: Path, digest: str) -> str | None:
    path = _cache_path(cache_dir, digest)
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError as exc:
        logger.warning("knowledge caption cache read failed %s: %r", path, exc)
    return None


def write_cached_caption(cache_dir: Path, digest: str, caption: str) -> None:
    path = _cache_path(cache_dir, digest)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(caption.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("knowledge caption cache write failed %s: %r", path, exc)


async def default_vision_caption(
    file_path: Path,
    *,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str | None:
    """Best-effort OpenAI-compatible vision caption. Returns None on any failure."""
    suffix = file_path.suffix.lower()
    mime = _MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        return None
    key = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")).strip()
    if not key:
        # NVIDIA / Gemini keys alone are not enough for this thin adapter.
        logger.debug(
            "knowledge llm caption skipped for %s — no OPENAI_API_KEY",
            file_path,
        )
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        logger.warning(
            "knowledge llm caption skipped for %s — openai package not installed",
            file_path,
        )
        return None
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        logger.warning("knowledge llm caption read failed %s: %r", file_path, exc)
        return None
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    client = AsyncOpenAI(
        api_key=key,
        base_url=base_url.rstrip("/") if base_url else None,
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _CAPTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            max_tokens=120,
        )
    except Exception as exc:
        logger.warning("knowledge llm caption failed for %s: %r", file_path, exc)
        return None
    try:
        text = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError) as exc:
        logger.warning(
            "knowledge llm caption malformed response for %s: %r", file_path, exc
        )
        return None
    return text or None


async def resolve_image_caption(
    *,
    rel_path: str,
    file_path: Path,
    mode: CaptionMode,
    cache_dir: Path,
    caption_model: str | None = None,
    vision_fn: VisionCaptionFn | None = None,
) -> str | None:
    """Resolve caption text for an image path.

    - ``off`` → None (caller should skip indexing)
    - ``path`` → deterministic path caption
    - ``llm`` → cached vision caption, falling back to path caption
    """
    if file_path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    normalized = (mode or "path").strip().lower()
    if normalized == "off":
        return None
    stub = path_caption(rel_path)
    if normalized != "llm":
        return stub

    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        logger.warning("knowledge image read failed %s: %r", file_path, exc)
        return stub
    model = (caption_model or "").strip() or "gpt-4o-mini"
    # Mix the model into the cache key so switching caption_model invalidates
    # previously cached captions instead of silently reusing stale ones.
    digest = content_hash(f"{model}\n".encode() + raw)
    cached = read_cached_caption(cache_dir, digest)
    if cached:
        return cached

    caption: str | None = None
    if vision_fn is not None:
        try:
            caption = await vision_fn(file_path, stub)
        except Exception as exc:
            logger.warning("knowledge vision_fn failed for %s: %r", file_path, exc)
            caption = None
    else:
        caption = await default_vision_caption(file_path, model=model)

    if caption and caption.strip():
        text = caption.strip()
        # Keep path tokens so basename search still works after LLM rewrite.
        if rel_path not in text and Path(rel_path).name not in text:
            text = f"{stub}\n{text}"
        write_cached_caption(cache_dir, digest, text)
        return text
    return stub


__all__ = [
    "CaptionMode",
    "VisionCaptionFn",
    "default_vision_caption",
    "path_caption",
    "read_cached_caption",
    "resolve_image_caption",
    "write_cached_caption",
]
