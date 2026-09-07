"""Shared tool definitions for the v2 harness.

`ToolDef` is referenced by ``monkeybot.core.llm.provider`` and MCP/context layers per
architecture docs; defining it once avoids import cycles across those modules.

See Story 2 ``types_tools`` task for canonical fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDef:
    """Tool schema for providers plus harness-only execution flags.

    Model-facing payloads must use :meth:`to_model_schema` (name, description,
    input_schema only). ``parallel_safe``, ``read_only``, and ``doom_loop_exempt``
    are harness metadata and must not be sent to providers or recorded as model
    tool schemas in transcripts.

    ``parallel_safe`` marks tools the harness may execute together in one batch.
    That is a concurrency hint, not a read-only guarantee.

    ``read_only`` marks tools that do not mutate the workspace or user machine.
    The verifier block inspector uses this flag, not ``parallel_safe``.

    ``doom_loop_exempt`` skips the identical name+args doom-loop guard for tools
    that are expected to repeat with the same arguments (e.g. ``loop_status``
    polling).
    """

    name: str
    description: str
    input_schema: dict[str, object]
    parallel_safe: bool = False
    doom_loop_exempt: bool = False
    read_only: bool = False

    def to_model_schema(self) -> dict[str, Any]:
        """JSON shape advertised to the model (excludes harness-only flags)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
