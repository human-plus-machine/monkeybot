#!/usr/bin/env python3
"""Dump the public MCP tool schema (names, docs, params) as JSON.

Used as a byte-stable gate so refactors of server.py cannot change the
model-visible surface. Write tests/fixtures/tool_surface.json with:

    uv run python scripts/dump_tool_surface.py > tests/fixtures/tool_surface.json
"""

from __future__ import annotations

import asyncio
import json
import sys

from browser_mcp.server import mcp


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


def main() -> int:
    json.dump(_surface(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
