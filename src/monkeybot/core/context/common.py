"""Shared context-shaping types and helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from monkeybot.core.types.content_blocks import ContentBlock, Text

ContextPressureTier = Literal["light", "moderate", "aggressive"]


def text_from_blocks(blocks: Sequence[ContentBlock]) -> str:
    return "".join(block.text for block in blocks if isinstance(block, Text))
