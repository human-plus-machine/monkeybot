"""Tavily web search backend (requires TAVILY_API_KEY)."""

from __future__ import annotations

from monkeybot.web_search.protocol import SearchResult

_API_URL = "https://api.tavily.com/search"


class TavilyBackend:
    name = "tavily"

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("TAVILY_API_KEY must be non-empty")
        self._api_key = api_key

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for the Tavily backend. "
                "Install with: uv add httpx"
            ) from exc

        payload = {
            "query": query,
            "max_results": max_results,
            "api_key": self._api_key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
                score=r.get("score"),
            )
            for r in data.get("results", [])
        ]
