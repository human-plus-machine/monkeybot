"""CustomTool wrapper that exposes a WebSearchBackend as a harness tool."""

from __future__ import annotations

import json
import os

from monkeybot.core.types.types_tools import ToolDef
from monkeybot.web_search.protocol import WebSearchBackend

_WEB_SEARCH_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query."},
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return (default 5).",
        },
    },
    "required": ["query"],
}


class WebSearchTool:
    """Adapts a :class:`WebSearchBackend` to the :class:`~monkeybot.core.context.CustomTool` protocol."""

    def __init__(self, backend: WebSearchBackend) -> None:
        self._backend = backend
        self.tool_def = ToolDef(
            "web_search",
            f"Search the web using {backend.name}. Returns titles, URLs, and text snippets.",
            _WEB_SEARCH_SCHEMA,
        )

    async def execute(self, args: dict[str, object]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"ok": False, "error": "web_search requires a non-empty query."})

        default_max = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "5"))
        raw_max = args.get("max_results")
        if isinstance(raw_max, (int, float, str)):
            try:
                max_results = int(raw_max)
            except (TypeError, ValueError):
                max_results = default_max
        else:
            max_results = default_max

        results = await self._backend.search(query, max_results=max_results)
        items = [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            | ({"score": r.score} if r.score is not None else {})
            for r in results
        ]
        return json.dumps({"ok": True, "query": query, "results": items}, ensure_ascii=False)
