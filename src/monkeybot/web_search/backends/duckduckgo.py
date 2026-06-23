"""DuckDuckGo web search backend (zero-config default via the ``ddgs`` package)."""

from __future__ import annotations

import asyncio

from monkeybot.web_search.protocol import SearchResult


class DuckDuckGoBackend:
    name = "duckduckgo"

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise ImportError(
                "ddgs is required for the DuckDuckGo backend. "
                "Install with: uv add ddgs"
            ) from exc

        raw = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
            )
            for r in raw
        ]
