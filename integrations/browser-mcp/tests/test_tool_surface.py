"""Golden MCP tool-surface snapshot. Fail if any tool name, docstring, or schema drifts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from browser_mcp.server import mcp

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "tool_surface.json"


def _surface() -> dict[str, object]:
    tools = asyncio.run(mcp.list_tools())
    return {
        "instructions": mcp.instructions,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
            }
            for t in sorted(tools, key=lambda t: t.name)
        ],
    }


def test_tool_surface_matches_golden() -> None:
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    actual = _surface()
    assert actual == expected
    assert len(actual["tools"]) == 32
