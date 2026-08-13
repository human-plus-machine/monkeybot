"""Version ranges shared by CLI packaging and agent scaffolding."""

from __future__ import annotations

# Floor matches published ``monkeybot-cli`` → ``monkeybot[cli]`` bound.
# Raise together when scaffolding or gateway APIs break compatibility.
COMPATIBLE_CORE_RANGE = ">=3.0.0,<4"
