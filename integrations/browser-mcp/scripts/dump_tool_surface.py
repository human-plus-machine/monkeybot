#!/usr/bin/env python3
"""Dump the public MCP tool schema (names, docs, params) as JSON.

Used as a byte-stable gate so refactors cannot change the model-visible
surface. Write tests/fixtures/tool_surface.json with:

    uv run python scripts/dump_tool_surface.py > tests/fixtures/tool_surface.json
"""

from __future__ import annotations

import json
import sys

from browser_mcp.server import tool_surface


def main() -> int:
    json.dump(tool_surface(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
