"""Memory is always on when a palace URI is configured.

``memory.enabled``, ``memory_hook.enabled``, and ``MONKEYBOT_MEMORY_HOOK_ENABLED``
are retired. Callers that still import :func:`memory_enabled_from_config` get
``True``.
"""

from __future__ import annotations


def memory_enabled_from_config(config_path: str | None = None) -> bool:
    """Always ``True``. The capture/recall kill switch was removed."""
    del config_path
    return True
