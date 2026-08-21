"""Version ranges shared by CLI packaging and agent scaffolding."""

from __future__ import annotations

import re

# Floor matches published ``monkeybot-cli`` → ``monkeybot[cli]`` bound.
# Raise together when scaffolding or gateway APIs break compatibility.
# Form must stay ``>=LOWER,<UPPER`` so the agent probe can derive bounds.
COMPATIBLE_CORE_RANGE = ">=3.0.0,<4"


def _bounds_from_range(range_: str) -> tuple[str, str]:
    """Parse inclusive lower / exclusive upper versions from ``COMPATIBLE_CORE_RANGE``."""
    match = re.fullmatch(r">\s*=\s*([^,]+)\s*,\s*<\s*(.+)", range_.strip())
    if match is None:
        raise ValueError(
            f"COMPATIBLE_CORE_RANGE must be '>=LOWER,<UPPER', got {range_!r}"
        )
    lower, upper = match.group(1).strip(), match.group(2).strip()
    if not lower or not upper:
        raise ValueError(
            f"COMPATIBLE_CORE_RANGE must be '>=LOWER,<UPPER', got {range_!r}"
        )
    return lower, upper


COMPATIBLE_CORE_LOWER_VERSION, COMPATIBLE_CORE_UPPER_VERSION = _bounds_from_range(
    COMPATIBLE_CORE_RANGE
)
