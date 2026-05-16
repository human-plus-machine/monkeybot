"""Firecrawl web search backend (requires FIRECRAWL_API_KEY)."""

from __future__ import annotations

from monkeybot.web_search.protocol import SearchResult

_API_URL = "https://api.firecrawl.dev/v1/search"


class FirecrawlBackend:
    name = "firecrawl"

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("FIRECRAWL_API_KEY must be non-empty")
        self._api_key = api_key

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for the Firecrawl backend. "
                "Install with: uv add httpx"
            ) from exc

        payload = {"query": query, "limit": max_results}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results: list[SearchResult] = []
        for r in data.get("data", []):
            results.append(
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("description", ""),
                )
            )
        return results
