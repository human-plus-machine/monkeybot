"""Sanitize text before sending to embedding APIs.

NVIDIA (and similar) embed endpoints mis-detect inline ``data:image...`` /
base64 media as multimodal image inputs and return 503 errors like
``image inputs require VLM serving``. One poisoned chunk must not abort a
whole-repo embed flush.
"""

from __future__ import annotations

import re

# Match from data:image/... (or data:application/...) through the payload,
# stopping at whitespace / quotes / brackets typical of source literals.
_DATA_URI_RE = re.compile(
    r"data:(?:image|application)/[^\s\"'`\]]+",
    re.IGNORECASE,
)

_OMIT = "[embedded-media omitted]"


def sanitize_embed_text(text: str) -> str:
    """Replace inline data-URIs with a short placeholder for embedding."""
    if not text or "data:" not in text:
        return text or ""
    return _DATA_URI_RE.sub(_OMIT, text)
