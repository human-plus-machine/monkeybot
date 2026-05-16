"""Shared tool definitions for the v2 harness.

`ToolDef` is referenced by ``monkeybot.core.llm.provider`` and MCP/context layers per
architecture docs; defining it once avoids import cycles across those modules.

See Story 2 ``types_tools`` task for canonical fields.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDef:
    """Tool schema surfaced to providers (JSON-schema ``input_schema``)."""

    name: str
    description: str
    input_schema: dict[str, object]
