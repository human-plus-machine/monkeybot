"""Version ranges shared by CLI packaging and agent scaffolding."""

from __future__ import annotations

# Floor matches published ``monkeybot-cli`` → ``monkeybot[cli]`` bound.
# Raise together when scaffolding or gateway APIs break compatibility.
COMPATIBLE_CORE_RANGE = ">=3.0.0,<4"

# Inclusive lower / exclusive upper versions for the stdlib agent probe.
# Keep aligned with ``COMPATIBLE_CORE_RANGE`` (SpecifierSet.contains defaults).
COMPATIBLE_CORE_LOWER_VERSION = "3.0.0"
COMPATIBLE_CORE_UPPER_VERSION = "4"
