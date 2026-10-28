"""Fixed web-search backend for scripted scenarios.

Records every query. Returns the configured results, sliced to ``max_results``.
Scenarios that name ``WEB_SEARCH_BACKEND=fake`` assert these titles so a real
backend cannot pass.
"""

from __future__ import annotations

from monkeybot.web_search.protocol import SearchResult


class FakeWebSearchBackend:
    """In-memory backend. ``queries`` is ``(query, max_results)`` in call order."""

    name = "fake"

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.results: list[SearchResult] = (
            list(results) if results is not None else list(_DEFAULT_RESULTS)
        )
        self.queries: list[tuple[str, int]] = []

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        limit = max(0, max_results)
        self.queries.append((query, limit))
        return list(self.results[:limit])


_DEFAULT_RESULTS: tuple[SearchResult, ...] = (
    SearchResult(
        title="Rust 1.80",
        url="https://example.test/rust-1.80",
        snippet="Several new stabilized APIs.",
        score=0.9,
    ),
    SearchResult(
        title="Release notes",
        url="https://example.test/notes",
        snippet="The 1.80 release notes.",
    ),
    SearchResult(
        title="Third hit",
        url="https://example.test/third",
        snippet="Dropped when max_results is 2.",
    ),
)
