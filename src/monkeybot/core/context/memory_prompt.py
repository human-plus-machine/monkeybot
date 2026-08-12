"""Memory prompt selection: wake-up lines already on TurnContext (no curator)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from monkeybot.core.context import TurnContext


@dataclass(frozen=True)
class MemoryPromptSelection:
    """Memory lines injected into the volatile system-prompt tail."""

    lines: list[str]


def memory_index_fingerprint(lines: list[str]) -> str:
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def prepare_memory_for_prompt(ctx: TurnContext) -> MemoryPromptSelection:
    return MemoryPromptSelection(lines=list(ctx.memory_index))
