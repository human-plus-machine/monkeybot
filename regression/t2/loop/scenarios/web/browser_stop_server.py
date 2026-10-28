"""Stdio MCP server for T2-WEB-05. Exposes ``browser_stop`` and nothing else.

Writes ``MB_BROWSER_STOP_MARKER`` only when that path is absolute and its
parent directory already exists. The loop driver expands ``{workspace}`` into
the MCP env overlay before this process starts.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("browser")


@mcp.tool()
def browser_stop() -> str:
    """Stop the remote browser session and record that this call ran."""
    raw = os.environ.get("MB_BROWSER_STOP_MARKER", "").strip()
    if raw:
        path = Path(raw)
        if path.is_absolute() and path.parent.is_dir():
            path.write_text("browser_stop\n", encoding="utf-8")
    return "stopped"


if __name__ == "__main__":
    mcp.run(transport="stdio")
