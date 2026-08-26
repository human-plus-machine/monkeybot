"""Pluggable web search for monkeybot.

Usage
-----
Call :func:`build_backend` once at startup (reads env vars).  Pass the
returned backend to :class:`~monkeybot.core.tools.core_tool_executor.CoreToolExecutor`
via ``extra_tools``.  ``None`` means web search is disabled.

Bring-your-own backend
-----------------------
Implement :class:`~monkeybot.web_search.protocol.WebSearchBackend` and wrap it
in a :class:`~monkeybot.core.context.CustomTool`::

    class MyBackend:
        name = "my_search"
        async def search(self, query, *, max_results=5): ...

    tool = web_search_custom_tool(MyBackend())
    executor = CoreToolExecutor(..., extra_tools=[tool])
"""

from __future__ import annotations

import os

from monkeybot.core.config.snapshot import current_env
from monkeybot.web_search.backends.duckduckgo import DuckDuckGoBackend
from monkeybot.web_search.backends.firecrawl import FirecrawlBackend
from monkeybot.web_search.backends.tavily import TavilyBackend
from monkeybot.web_search.protocol import SearchResult, WebSearchBackend
from monkeybot.web_search.tool import WebSearchTool

__all__ = [
    "SearchResult",
    "WebSearchBackend",
    "DuckDuckGoBackend",
    "TavilyBackend",
    "FirecrawlBackend",
    "WebSearchTool",
    "build_backend",
]


def build_backend(backend_name: str | None = None) -> WebSearchBackend | None:
    """Construct the configured backend from environment variables.

    ``WEB_SEARCH_BACKEND`` selects the implementation:

    * ``duckduckgo`` (default) — no API key required; uses the ``ddgs`` package.
    * ``tavily`` — requires ``TAVILY_API_KEY``.
    * ``firecrawl`` — requires ``FIRECRAWL_API_KEY``.
    * ``none`` — disables web search entirely.

    Returns ``None`` when ``WEB_SEARCH_BACKEND=none``.
    ``backend_name`` overrides the env var (config-reload snapshot).
    """
    if backend_name is None:
        backend_name = current_env("WEB_SEARCH_BACKEND", "duckduckgo")
    backend_name = backend_name.lower().strip()

    if backend_name == "none":
        return None

    if backend_name == "tavily":
        api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not api_key:
            raise ValueError("WEB_SEARCH_BACKEND=tavily requires TAVILY_API_KEY to be set.")
        return TavilyBackend(api_key=api_key)

    if backend_name == "firecrawl":
        api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        if not api_key:
            raise ValueError("WEB_SEARCH_BACKEND=firecrawl requires FIRECRAWL_API_KEY to be set.")
        return FirecrawlBackend(api_key=api_key)

    if backend_name == "duckduckgo":
        return DuckDuckGoBackend()

    raise ValueError(
        f"Unknown WEB_SEARCH_BACKEND={backend_name!r}. "
        "Choose one of: duckduckgo, tavily, firecrawl, none."
    )
