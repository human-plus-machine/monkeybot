"""Memory capture/recall kill switch.

Precedence (first match wins):

1. ``MONKEYBOT_MEMORY_HOOK_ENABLED`` in the process environment
2. YAML ``memory.enabled``
3. YAML ``memory_hook.enabled`` (legacy alias)
4. Default ``true``

``memory.enabled`` is YAML-only (not in ``ENV_MAP``) so the legacy
``memory_hook.enabled`` key cannot overwrite it via env mapping.
"""

from __future__ import annotations

import os

from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

_FALSE = frozenset({"0", "false", "no", "off"})
_TRUE = frozenset({"1", "true", "yes", "on"})


def _parse_bool(raw: object, *, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"{field} must be true or false, got {raw!r}")


def memory_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether MemPalace capture, recall, and search teaching are enabled."""
    env = os.environ.get("MONKEYBOT_MEMORY_HOOK_ENABLED", "").strip().lower()
    if env in _FALSE:
        return False
    if env in _TRUE:
        return True
    _, doc = load_monkeybot_yaml_dict(config_path)
    memory = doc.get("memory") if isinstance(doc, dict) else None
    if isinstance(memory, dict) and "enabled" in memory:
        return _parse_bool(memory.get("enabled"), field="memory.enabled")
    hook = doc.get("memory_hook") if isinstance(doc, dict) else None
    if isinstance(hook, dict) and "enabled" in hook:
        return _parse_bool(hook.get("enabled"), field="memory_hook.enabled")
    return True
