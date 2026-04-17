"""Harness middleware modules.

Middleware objects are simple async callables ``mw(state, next_call)`` composed by the
assembler in the frozen order defined in ``docs/agent-harness.md``.
"""

# BEGIN harness-extensibility story 5
from ._identity_cache import IdentityCache
from .identity_resolution import IdentityResolutionMW

__all__ = [
    "IdentityCache",
    "IdentityResolutionMW",
]
# END harness-extensibility story 5
