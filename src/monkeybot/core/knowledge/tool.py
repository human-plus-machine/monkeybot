"""Serialize recall hits for the agent tool surface."""

from __future__ import annotations

from typing import Any

from monkeybot.core.knowledge.types import RecallHit


def serialize_recall_result(
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


def legacy_search_memory_shape(recall_payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt recall hits into the older ``search_memory`` hit shape for migration."""
    hits = []
    for h in recall_payload.get("hits") or []:
        if not isinstance(h, dict):
            continue
        hits.append(
            {
                "path": h.get("path"),
                "snippet": h.get("snippet") or "",
                "match_offset": 0,
                "source_type": h.get("source_type"),
                "score": h.get("score"),
                "span": h.get("span"),
                "via": h.get("via"),
                "links": h.get("links"),
            }
        )
    return {
        "ok": True,
        "query": recall_payload.get("query", ""),
        "hits": hits,
        "truncated": False,
    }


__all__ = ["legacy_search_memory_shape", "serialize_recall_result"]
