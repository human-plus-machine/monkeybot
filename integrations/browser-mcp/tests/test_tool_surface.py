"""Golden MCP tool-surface snapshot. Fail if any tool name, docstring, or schema drifts."""

from __future__ import annotations

import json
from pathlib import Path

from browser_mcp.server import tool_surface

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "tool_surface.json"


def test_tool_surface_matches_golden() -> None:
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    actual = tool_surface()
    assert actual == expected
    assert len(actual["tools"]) == 31
