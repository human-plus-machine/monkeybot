"""Serialize search hits for the agent tool surface."""

from __future__ import annotations

from typing import Any

from monkeybot.core.knowledge.types import RecallHit


def serialize_search_result(
    query: str,
    hits: list[RecallHit],
    *,
    limit: int,
    stale: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "query": query,
        "limit": limit,
        "hits": [h.to_dict() for h in hits],
        "count": len(hits),
    }
    if stale:
        out["stale"] = True
    return out


__all__ = ["serialize_search_result"]
