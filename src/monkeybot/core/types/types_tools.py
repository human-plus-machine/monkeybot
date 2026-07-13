"""Shared tool definitions for the v2 harness.

`ToolDef` is referenced by ``monkeybot.core.llm.provider`` and MCP/context layers per
architecture docs; defining it once avoids import cycles across those modules.

See Story 2 ``types_tools`` task for canonical fields.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDef:
    """Tool schema surfaced to providers (JSON-schema ``input_schema``).

    ``parallel_safe`` marks read-only (or otherwise concurrent-safe) tools that
    the harness may execute together in one batch. Mutating tools stay serial
    unless explicitly opted in. Providers ignore this field.

    ``doom_loop_exempt`` skips the identical name+args doom-loop guard for tools
    that are expected to repeat with the same arguments (e.g. ``loop_status``
    polling). Providers ignore this field.
    """

    name: str
    description: str
    input_schema: dict[str, object]
    parallel_safe: bool = False
    doom_loop_exempt: bool = False
