"""Best-effort token counting. Uses tiktoken if available; otherwise chars/4 fallback."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Sequence


@lru_cache(maxsize=16)
def _encoder(model: str) -> Any:
    try:
        import tiktoken  # type: ignore

        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except ImportError:
        return None


def count_tokens(model: str, content: str | Sequence[Any]) -> int:
    text = _coerce_text(content)
    enc = _encoder(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _coerce_text(content: str | Sequence[Any]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            if "content" in item:
                c = item["content"]
                parts.append(c if isinstance(c, str) else str(c))
                continue
            parts.append(str(item))
            continue
        for attr in ("content", "text"):
            if hasattr(item, attr):
                val = getattr(item, attr)
                if isinstance(val, str):
                    parts.append(val)
                    break
        else:
            parts.append(str(item))
    return "\n".join(parts)
